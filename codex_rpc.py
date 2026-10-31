"""Bounded client for Codex's official stdio account-management RPCs.

Each client owns a fresh app-server process. Account metadata is not proof that
a login still works; callers must complete rate_limits() before marking it valid.
No model turns, token injection, shell commands, or remote error bodies are used.
"""
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time


AUTH_ENV = frozenset({
    'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_AUTH_TOKEN',
    'CHATGPT_ACCESS_TOKEN', 'OPENAI_BASE_URL', 'OPENAI_API_BASE',
    'CODEX_SQLITE_HOME', 'CODEX_AUTHAPI_BASE_URL', 'CODEX_APP_SERVER_LOGIN_CLIENT_ID',
    'CODEX_APP_SERVER_LOGIN_ISSUER', 'CODEX_APP_SERVER_DEV_OPEN_APP_URL',
    'CODEX_REFRESH_TOKEN_URL_OVERRIDE', 'CODEX_REVOKE_TOKEN_URL_OVERRIDE',
})
MAX_LINE = 2 * 1024 * 1024
MAX_EVENTS = 256


class RpcError(RuntimeError):
    """Safe, displayable failure. Never retains a remote body or credential."""
    def __init__(self, message, kind='unavailable', code=None):
        super().__init__(message)
        self.kind = kind
        self.code = code


def rpc_error(error):
    """Classify a remote error without copying it into logs, exceptions or UI."""
    if not isinstance(error, dict):
        return RpcError('Codex returned an invalid account response.')
    code = error.get('code')
    code = code if isinstance(code, int) else None
    message = error.get('message')
    message = message[:8192].lower() if isinstance(message, str) else ''
    if code == -32601:
        return RpcError('This Codex version does not support the requested account operation.', code=code)
    if re.search(r'\b429\b', message) or 'too many requests' in message or 'rate limited' in message:
        return RpcError('Codex usage checks are rate-limited. Retrying later.', 'rate_limited', code)
    if any(word in message for word in ('usage limit', 'quota exhausted', 'quota exceeded', 'credits depleted')):
        return RpcError('This Codex account has reached a usage limit.', 'limited', code)
    if re.search(r'\b(401|403)\b', message) or any(word in message for word in (
            'authentication required', 'sign in', 'unauthorized', 'refresh token', 'refresh_token', 'invalid_grant')):
        return RpcError('Codex sign-in could not be verified. Refresh the login or sign in again.', 'auth', code)
    return RpcError('Codex could not complete the account check. Retry later.', code=code)


def environment(codex_home, source=None):
    env = dict(os.environ if source is None else source)
    for key in AUTH_ENV:
        env.pop(key, None)
    env['CODEX_HOME'] = str(Path(codex_home).absolute())
    return env


def executable_path():
    executable = shutil.which('codex')
    if not executable:
        raise RpcError('Codex CLI is not installed or is not on PATH.')
    return executable


def _toml_value(value):
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, (str, bool, int)):
        return json.dumps(value)
    if isinstance(value, float) and math.isfinite(value):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_toml_value(item) for item in value) + ']'
    raise ValueError('Unsupported Codex configuration value.')


class CodexRPC:
    def __init__(self, codex_home, executable=None, extra_config=None):
        self.codex_home = Path(codex_home).absolute()
        self.executable = str(executable) if executable is not None else None
        self.extra_config = dict(extra_config or {})
        self.process = None
        self._work = None
        self._reader = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._events = queue.Queue(maxsize=MAX_EVENTS)
        self._counter = 0
        self._failure = None
        self._closed = False

    def __enter__(self):
        if self.process is not None or self._closed:
            raise RpcError('This Codex account connection cannot be reused.')
        try:
            self.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._work = tempfile.TemporaryDirectory(prefix='codex-account-check-')
            command = [self.executable or executable_path(), '--no-daemon', 'app-server', '--listen', 'stdio://']
            for key, value in self.extra_config.items():
                if not re.fullmatch(r'[A-Za-z0-9_.-]+', key):
                    raise ValueError('Invalid Codex configuration key.')
                command.extend(['-c', key + '=' + _toml_value(value)])
            self.process = subprocess.Popen(command, cwd=self._work.name,
                                            env=environment(self.codex_home), stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, encoding='utf-8', errors='strict',
                                            start_new_session=True)
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
            self.call('initialize', {'clientInfo': {'name': 'claude_accounts',
                      'title': 'Claude Accounts', 'version': '1'},
                      'capabilities': {'experimentalApi': True}}, timeout=15)
            self.notify('initialized', {})
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, ValueError)):
                raise RpcError('Codex account helper could not be started. Check the CLI installation.') from None
            raise

    def __exit__(self, *_):
        self.close()

    def _fail(self, message):
        with self._pending_lock:
            if self._failure is None:
                self._failure = message
            for pending in self._pending.values():
                try:
                    pending.put_nowait(None)
                except queue.Full:
                    pass
        try:
            self._events.put_nowait(None)
        except queue.Full:
            pass

    def _read(self):
        try:
            while not self._closed:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or not line.endswith('\n'):
                    self._fail('Codex returned an oversized account response.')
                    return
                try:
                    message = json.loads(line)
                except ValueError:
                    self._fail('Codex returned an invalid account response.')
                    return
                if not isinstance(message, dict):
                    self._fail('Codex returned an invalid account response.')
                    return
                if 'method' in message and 'id' in message:
                    # This account-only client does not execute server tool requests.
                    self._write({'id': message['id'], 'error': {'code': -32601,
                                 'message': 'Unsupported client request'}})
                elif 'id' in message:
                    with self._pending_lock:
                        pending = self._pending.get(message['id']) if isinstance(message['id'], int) else None
                        if pending is not None:
                            try:
                                pending.put_nowait(message)
                            except queue.Full:
                                pass
                elif isinstance(message.get('method'), str):
                    try:
                        self._events.put_nowait(message)
                    except queue.Full:
                        self._fail('Codex sent too many account events. Reopen the account check.')
                        return
        except (OSError, UnicodeError, ValueError, RpcError):
            pass
        finally:
            self._fail('Codex account helper stopped before completing the request.')

    def _write(self, message):
        try:
            payload = json.dumps(message, separators=(',', ':')) + '\n'
            with self._write_lock:
                if self._closed or self.process is None or self.process.poll() is not None:
                    raise RpcError('Codex account helper is not running.')
                self.process.stdin.write(payload)
                self.process.stdin.flush()
        except (OSError, ValueError, TypeError):
            raise RpcError('Codex account helper could not receive the request.') from None

    def call(self, method, params=None, timeout=30):
        with self._pending_lock:
            if self._failure:
                raise RpcError(self._failure)
            self._counter += 1
            request_id = self._counter
            pending = queue.Queue(maxsize=1)
            self._pending[request_id] = pending
        try:
            self._write({'id': request_id, 'method': method, 'params': params})
            try:
                response = pending.get(timeout=max(0, timeout))
            except queue.Empty:
                raise RpcError('Codex account request timed out. Retry later.') from None
            if response is None:
                raise RpcError(self._failure or 'Codex account helper stopped.')
            if 'error' in response:
                raise rpc_error(response['error'])
            if 'result' not in response:
                raise RpcError('Codex returned an invalid account response.')
            return response['result']
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method, params=None):
        self._write({'method': method, 'params': params})

    def read_account(self, refresh=False):
        result = self.call('account/read', {'refreshToken': bool(refresh)})
        if not isinstance(result, dict) or (result.get('account') is not None and not isinstance(result['account'], dict)):
            raise RpcError('Codex returned an invalid account response.')
        return result

    def rate_limits(self):
        result = self.call('account/rateLimits/read', {'excludeResetCreditDetails': True})
        if not isinstance(result, dict) or not isinstance(result.get('rateLimits'), dict):
            raise RpcError('Codex returned an invalid usage response.')
        return result

    def login_start(self, device_code=False):
        kind = 'chatgptDeviceCode' if device_code else 'chatgpt'
        result = self.call('account/login/start', {'type': kind})
        required = ('loginId', 'verificationUrl', 'userCode') if device_code else ('loginId', 'authUrl')
        if not isinstance(result, dict) or result.get('type') != kind or any(
                not isinstance(result.get(key), str) or not result[key] for key in required):
            raise RpcError('Codex could not start sign-in. Retry the login.')
        return result

    def wait_login(self, login_id, timeout=300):
        deadline = time.monotonic() + timeout
        while True:
            if self._failure:
                raise RpcError(self._failure)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = self._events.get(timeout=remaining)
            except queue.Empty:
                break
            if event is None:
                raise RpcError(self._failure or 'Codex account helper stopped.')
            params = event.get('params', {})
            if event.get('method') == 'account/login/completed' and isinstance(params, dict) and params.get('loginId') == login_id:
                if params.get('success') is True:
                    return {'loginId': login_id, 'success': True}
                raise RpcError('Codex sign-in did not complete. Retry browser or device sign-in.', 'auth')
        try:
            self.cancel(login_id, timeout=5)
        except RpcError:
            pass
        raise RpcError('Codex sign-in timed out. Start sign-in again.')

    def cancel(self, login_id, timeout=5):
        return self.call('account/login/cancel', {'loginId': login_id}, timeout=timeout)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._fail('Codex account connection was closed.')
        process = self.process
        if process is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                for signum in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        break
                    try:
                        process.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            if self._reader:
                self._reader.join(timeout=1)
                if self._reader.is_alive():
                    # A child can retain stdout after its parent exits.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self._reader.join(timeout=1)
            if process.stdout and (not self._reader or not self._reader.is_alive()):
                process.stdout.close()
        if self._work:
            self._work.cleanup()
            self._work = None
