"""Refresh expired subscription credentials through the official CLI only."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from desktop_profiles import atomic_json


def credential_id(credential):
    # An opaque generation identifier, never a credential that can authenticate.
    value = str(credential.get('accessToken', '')) + ':' + str(credential.get('expiresAt', ''))
    return hashlib.sha256(value.encode()).hexdigest()


def environment(label, home, source=None):
    env = dict(os.environ if source is None else source)
    for key in ('CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN', 'CLAUDE_CODE_OAUTH_SCOPES',
                'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_PROFILE',
                'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_SIMPLE'):
        env.pop(key, None)
    if label == 'a':
        env.pop('CLAUDE_CONFIG_DIR', None)
    else:
        env['CLAUDE_CONFIG_DIR'] = str(home / ('.claude-acct-' + label))
    env['CLAUDE_CODE_SAFE_MODE'] = '1'
    return env


def stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except ProcessLookupError:
            process.wait(timeout=2)


def probe(label, home, cancel, timeout=45):
    executable = shutil.which('claude')
    if not executable:
        return 'unavailable'
    # Empty working directory prevents a refresh from joining a project session.
    # Safe mode retains subscription auth; --bare would disable it.
    command = [executable, '--safe-mode', '-p', 'Reply only OK.', '--tools', '',
               '--no-session-persistence', '--system-prompt', 'Reply only OK.']
    with tempfile.TemporaryDirectory(prefix='claude-login-refresh-') as directory, tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=directory, env=environment(label, home),
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel.wait(.2):
                    return 'cancelled'
                if time.monotonic() >= deadline:
                    return 'timeout'
            # Never forward CLI output, auth URLs or credentials to the UI/logs.
            output.seek(0)
            message = output.read(65536).decode(errors='replace').lower()
            if any(s in message for s in ('invalid_grant', 'login expired', 'please run /login',
                                          'please log in', 'not logged in', 'token revoked',
                                          'oauth token has expired', 'authentication_error')):
                return 'auth'
            return 'finished'
        finally:
            stop(process)


def refresh_account(label, home, read_credentials, identity, cancel=None, force=False, runner=probe):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', label):
        raise ValueError('Invalid account label')
    cancel = cancel or threading.Event()
    config = None if label == 'a' else home / ('.claude-acct-' + label)
    root = home / '.claude-account-refresh'
    if root.is_symlink():
        raise RuntimeError('The login-refresh state directory must not be a symlink.')
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    cache = root / (label + '.json')
    with (root / (label + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'label': label, 'cli_state': 'refresh_failed', 'cli_detail': 'Another account manager is refreshing this login. Refresh accounts shortly.'}
        before = read_credentials(config)
        generation = credential_id(before)
        base = {'label': label, 'before_id': generation, 'refresh_id': generation}
        expires = before.get('expiresAt')
        expired = isinstance(expires, (int, float)) and expires <= time.time()*1000
        if expired and not force and cache.exists():
            try:
                old = json.loads(cache.read_text())
                if old.get('before_id') == generation:
                    return old
            except (OSError, ValueError):
                pass
        if cancel.is_set():
            return {**base, 'cli_state': 'refresh_failed', 'cli_detail': 'Background refresh cancelled. Choose Retry background refresh.'}
        # Save an attempt marker before spawning. Crashes/restarts cannot create a
        # loop of chargeable probe requests for the same expired credential.
        fallback = {**base, 'cli_state': 'refresh_failed', 'cli_detail': 'Background refresh did not finish. Choose Retry background refresh.', 'attempted_at': time.time()}
        if expired:
            atomic_json(cache, fallback)
            outcome = runner(label, home, cancel)
        else:
            outcome = 'already_refreshed'
        if outcome == 'cancelled':
            return fallback
        # A quota error or timeout may happen AFTER token renewal. Credentials,
        # not the model response or process exit code, determine success.
        try:
            credential = read_credentials(config)
            base['refresh_id'] = credential_id(credential)
            new_expires = credential.get('expiresAt')
            still_expired = isinstance(new_expires, (int, float)) and new_expires <= time.time()*1000
            if not still_expired:
                account, organization, email = identity(config)
                result = {**base, 'refresh_id': credential_id(credential), 'cli_state': 'ready',
                          'cli_detail': 'CLI login refreshed and verified.', 'email': email,
                          'account': account, 'organization': organization,
                          'expires_at': new_expires/1000 if isinstance(new_expires, (int, float)) else None}
                cache.unlink(missing_ok=True)
                return result
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            pass
        if outcome == 'auth':
            result = {**base, 'cli_state': 'sign_in_required',
                      'cli_detail': 'Automatic renewal failed: sign in to CLI again.'}
        else:
            detail = {'timeout': 'Background refresh timed out.', 'unavailable': 'Claude CLI was not found on PATH.'}.get(
                outcome, 'Could not refresh and verify this login. Check your connection or retry; sign in if renewal keeps failing.')
            result = {**base, 'cli_state': 'refresh_failed', 'cli_detail': detail}
        atomic_json(cache, result)
        return result


def helpers():
    spec = importlib.util.spec_from_file_location('desktop_sync_refresh', Path(__file__).with_name('desktop-sync.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper.read_credentials, helper.identity


class RefreshWorker:
    def __init__(self, label, generation, force=False):
        import queue
        self.label, self.generation = label, generation
        self.cancelled = threading.Event()
        self.events = queue.Queue()
        self.thread = threading.Thread(target=self.run, args=(force,), daemon=True)
        self.thread.start()

    def run(self, force):
        try:
            result = refresh_account(self.label, Path.home(), *helpers(), cancel=self.cancelled, force=force)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            result = {'label': self.label, 'before_id': self.generation, 'cli_state': 'refresh_failed',
                      'cli_detail': 'Background refresh failed. Choose Retry background refresh, or sign in to CLI.'}
        self.events.put(result)

    def close(self):
        self.cancelled.set()
        self.thread.join(timeout=5)
