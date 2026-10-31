"""Codex profiles with one history home and canonical, locked native logins."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

import codex_install
from codex_rpc import CodexRPC, RpcError, environment
from desktop_profiles import atomic_json

INTERVAL = 300
MARKER = 'codex-accounts-runtime-v1'


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def private_dir(path):
    if path.is_symlink():
        raise RuntimeError('A managed Codex directory must not be a symlink.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def validate(label):
    if not isinstance(label, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', label):
        raise RuntimeError('Use 1–32 lowercase letters, numbers, hyphens or underscores.')


def claims(token):
    try:
        part = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError, AttributeError):
        return {}


def identity(auth):
    """Local hints only. A successful native quota request verifies the login."""
    tokens = auth.get('tokens') or {}
    if not isinstance(tokens, dict):
        return {'kind': 'missing'}
    access, id_token = claims(tokens.get('access_token')), claims(tokens.get('id_token'))
    details = id_token.get('https://api.openai.com/auth') or access.get('https://api.openai.com/auth') or {}
    profile = id_token.get('https://api.openai.com/profile') or access.get('https://api.openai.com/profile') or {}
    details = details if isinstance(details, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    account = tokens.get('account_id') or details.get('chatgpt_account_id')
    user = details.get('chatgpt_user_id') or id_token.get('sub') or access.get('sub')
    mode = auth.get('auth_mode')
    kind = ('external' if mode not in (None, 'chatgpt', 'apikey') else
            'chatgpt' if account and tokens.get('access_token') else 'apikey' if auth.get('OPENAI_API_KEY') else 'missing')
    return {'account': account, 'user': user, 'email': id_token.get('email') or profile.get('email'),
            'plan': details.get('chatgpt_plan_type'), 'expires_at': access.get('exp'),
            'kind': kind}


def usage_windows(result):
    buckets = result.get('rateLimitsByLimitId') or {'codex': result.get('rateLimits')}
    windows, limited = [], result.get('ordinaryUsageAllowed') is False
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        limited = limited or bool(bucket.get('rateLimitReachedType')) or bucket.get('spendControlReached') is True
        for period in ('primary', 'secondary'):
            row = bucket.get(period)
            if not isinstance(row, dict):
                continue
            percent = row.get('usedPercent')
            if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or percent < 0:
                continue
            minutes = row.get('windowDurationMins')
            label = f'{minutes//1440}d' if isinstance(minutes, int) and minutes >= 1440 else f'{minutes//60}h' if isinstance(minutes, int) and minutes >= 60 else f'{minutes}m' if minutes else period.title()
            window_key = ('session' if period == 'primary' else 'weekly_all') if key == 'codex' else f'{key}:{period}'
            windows.append({'key': window_key, 'label': label if key == 'codex' else key+' '+label,
                            'percent': percent, 'resets_at': row.get('resetsAt'), 'window_minutes': minutes})
            limited = limited or percent >= 100
    return windows, limited


class Manager:
    def __init__(self, home=None, rpc_factory=CodexRPC):
        self.home = Path.home() if home is None else Path(home)
        self.store = self.home/'.codex-accounts'
        self.native = self.home/'.codex'
        self.shared = self.home/'.codex-rotate'
        self.user_data = self.store/'desktop-runtime'
        self.index_path = self.store/'index.json'
        self.journal = self.store/'switch.json'
        self.rpc_factory = rpc_factory

    @contextlib.contextmanager
    def locked(self):
        private_dir(self.store)
        path = self.store/'manager.lock'
        if path.is_symlink():
            raise RuntimeError('Unsafe Codex manager lock.')
        with path.open('a') as stream:
            path.chmod(0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield stream

    @contextlib.contextmanager
    def lease(self, exclusive=False):
        private_dir(self.store)
        path = self.store/'runtime.lock'
        if path.is_symlink():
            raise RuntimeError('Unsafe Codex runtime lock.')
        with path.open('a') as stream:
            path.chmod(0o600)
            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('CODEX_BUSY: A managed Codex session is running. Finish it before changing accounts.') from None
            yield stream

    def index(self):
        value = read_json(self.index_path, {'profiles': {}, 'removed': {}, 'active': None})
        if not isinstance(value, dict) or not isinstance(value.get('profiles'), dict):
            raise RuntimeError('Codex profile index is invalid. Restore its backup before continuing.')
        return value

    def profiles(self, include_removed=False):
        index = self.index()
        rows = dict(index['profiles'])
        if 'a' not in rows and 'a' not in index.get('removed', {}):
            # The original store stays canonical: no duplicate refresh token.
            rows['a'] = {'source': str(self.native), 'native': True}
        if include_removed:
            rows.update({k: v['profile'] for k, v in index.get('removed', {}).items()})
        return rows

    def profile(self, label):
        validate(label)
        row = self.profiles().get(label)
        if row is None:
            raise RuntimeError('No such Codex profile. Add or restore it first.')
        return row

    def source(self, label):
        path = Path(self.profile(label)['source'])
        if path != self.native and not path.is_relative_to(self.store/'profiles'):
            raise RuntimeError('The Codex profile has an invalid login location.')
        if path.is_symlink() or (path != self.native and not path.resolve().is_relative_to(self.store.resolve()/'profiles')):
            raise RuntimeError('The Codex login directory must not be a symlink.')
        return path

    def auth_store(self, label):
        """Read the configured native store without reading Keychain secrets."""
        source = self.source(label)
        try:
            config = tomllib.loads((source/'config.toml').read_text())
        except FileNotFoundError:
            config = {}
        except (OSError, ValueError):
            raise RuntimeError('Could not read this Codex profile configuration.') from None
        return config.get('cli_auth_credentials_store', 'file')

    def ensure_cli_supported(self, executable=None):
        executable = executable or codex_install.detect_cli(self.home)
        if not executable:
            raise RuntimeError('Install Codex CLI or Desktop first.')
        try:
            result = subprocess.run([str(executable), '--version'], capture_output=True, text=True, timeout=10)
            match = re.search(r'\bcodex-cli\s+(\d+)\.(\d+)\.(\d+)', result.stdout)
            supported = result.returncode == 0 and match and tuple(map(int, match.groups())) >= (0, 166, 0)
        except (OSError, subprocess.SubprocessError):
            supported = False
        if not supported:
            raise RuntimeError('Update Codex CLI to version 0.166.0 or newer before using managed accounts. Existing logins are unchanged.')
        return executable

    def auth(self, label):
        path = self.source(label)/'auth.json'
        if path.exists():
            info = path.stat()
            if not path.is_file() or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise RuntimeError('The Codex login file must be a regular, privately owned file.')
        value = read_json(path, {})
        if not isinstance(value, dict):
            raise RuntimeError('The Codex login file is invalid. Sign in again.')
        return value

    def create(self, label):
        validate(label)
        with self.locked():
            index = self.index()
            if label in self.profiles(include_removed=True):
                raise RuntimeError('That label is already in use. Select or restore the existing profile.')
            folder = self.store/'profiles'/label
            private_dir(folder.parent)
            if folder.exists() or folder.is_symlink():
                raise RuntimeError('A login directory already exists for that label. Choose another label to keep it intact.')
            private_dir(folder)
            (folder/'config.toml').write_text('cli_auth_credentials_store = "file"\n')
            index['profiles'][label] = {'source': str(folder), 'created_at': time.time()}
            atomic_json(self.index_path, index)

    def verify(self, label, force=False):
        source = self.source(label)
        store = self.auth_store(label)
        auth = self.auth(label) if store == 'file' else {}
        local = identity(auth)
        cache_path = self.store/'usage'/f'{label}.json'
        cache = read_json(cache_path, {})
        if not isinstance(cache, dict):
            cache = {}
        signature = hashlib.sha256(json.dumps([str(source), store, auth], sort_keys=True).encode()).hexdigest()
        now = time.time()
        if not force and cache.get('fingerprint') == signature and cache.get('next_check', 0) > now:
            return cache
        result = {**local, 'label': label, 'cli_state': 'unverified', 'cli_detail': '',
                  'checked_at': now, 'next_check': now+INTERVAL, 'fingerprint': signature,
                  'source': str(source), 'login_store': store, 'reusable': False}
        try:
            if not source.exists() or store == 'file' and not auth:
                result.update(cli_state='missing', cli_detail='Sign in once to use Codex CLI and Desktop.')
                return result
            if local.get('kind') == 'external':
                result.update(cli_state='external', requires_managed_login=True,
                              cli_detail='This login is managed by another application. Change login to sign in to a managed profile.')
                return result
            executable = codex_install.detect_cli(self.home)
            if not executable:
                raise RpcError('Install Codex CLI or Desktop first.')
            config = {} if self.profile(label).get('native') else {'cli_auth_credentials_store': 'file'}
            with self.rpc_factory(source, executable=executable, extra_config=config) as rpc:
                expiry = local.get('expires_at')
                metadata = rpc.read_account(refresh=force or isinstance(expiry, (int, float)) and expiry < now+INTERVAL)
                account = metadata.get('account')
                if not account:
                    result.update(cli_state='missing', cli_detail='Sign in to Codex to continue.')
                elif account.get('type') == 'apiKey':
                    result.update(cli_state='api_key' if store == 'file' else 'external', reusable=store == 'file',
                                  cli_detail='API-key login. Subscription quotas and rotation require ChatGPT sign-in.')
                elif account.get('type') != 'chatgpt':
                    result.update(cli_state='unverified', cli_detail='This authentication mode cannot participate in ChatGPT account rotation.')
                else:
                    quotas = rpc.rate_limits()
                    windows, limited = usage_windows(quotas)
                    latest = self.auth(label) if store == 'file' else {}
                    result.update(identity(latest))
                    if quotas.get('accountId') and result.get('account') and quotas['accountId'] != result['account']:
                        raise RpcError('Codex returned usage for a different account. Check this profile login.', 'auth')
                    previous = cache if cache.get('source') == str(source) else {}
                    expected = previous.get('expected_identity') or previous
                    drift = any(expected.get(key) and result.get(key) and expected[key] != result[key]
                                for key in ('account', 'user'))
                    result.update(email=account.get('email') or result.get('email'), plan=account.get('planType'),
                                  cli_state='drift' if drift else 'ready' if store == 'file' else 'external',
                                  cli_detail=('This profile now has a different login. Change login to reconcile it.' if drift else
                                              'Codex login verified.' if store == 'file' else
                                              'Existing '+str(store)+' login detected. Change login to create a reusable managed profile; the original login is kept.'),
                                  reusable=store == 'file' and not drift, requires_managed_login=store != 'file', limited=limited,
                                  fingerprint=hashlib.sha256(json.dumps([str(source), store, latest], sort_keys=True).encode()).hexdigest(),
                                  usage={'state': 'ready' if windows else 'unavailable', 'windows': windows,
                                         'checked_at': now, 'detail': '' if windows else 'No quota windows returned.'})
                    if drift:
                        result['expected_identity'] = {k: expected.get(k) for k in ('account', 'user')}
        except RpcError as error:
            result.update(cli_state='sign_in_required' if error.kind == 'auth' else 'unverified', cli_detail=str(error))
            old_usage = cache.get('usage', {})
            result['usage'] = {**old_usage, 'state': 'stale' if old_usage.get('windows') else 'unavailable', 'detail': str(error)}
            result['error_kind'] = error.kind
        private_dir(self.store)
        private_dir(cache_path.parent)
        atomic_json(cache_path, result)
        return result

    def prepare_runtime(self):
        if self.shared.exists():
            if self.shared.is_symlink() or read_json(self.shared/'.accounts-managed') != MARKER:
                raise RuntimeError('The Codex shared directory is not managed by this app. Move it before continuing.')
        else:
            private_dir(self.shared)
            atomic_json(self.shared/'.accounts-managed', MARKER)
            # One physical history home avoids split SQLite WALs and atomically
            # rewritten name/history indexes. Imports are handled separately.
            (self.shared/'config.toml').write_text('cli_auth_credentials_store = "file"\n')
        private_dir(self.shared)
        imported = self.shared/'.history-imported'
        if not imported.exists():
            import codex_history
            summary = (codex_history.import_history(self.native, self.shared) if self.native.is_dir() else
                       {'copied': 0, 'existing': 0, 'skipped': 0, 'conflicts': [], 'errors': []})
            if summary.get('errors'):
                raise RuntimeError('Local Codex history could not be imported completely. Retry before opening the managed runtime.')
            atomic_json(imported, summary)

    def desktop_state(self):
        import codex_desktop
        return codex_desktop.desktop_state(self.shared, self.user_data)

    def _link(self, name, target):
        path = self.shared/name
        temporary = self.shared/(name+'.accounts-new')
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        temporary.replace(path)

    def _activate(self, label, verified=None):
        if self.journal.exists():
            raise RuntimeError('Recover the interrupted Codex switch from Tools first.')
        self.ensure_cli_supported()
        checked = verified or self.verify(label)
        source = self.source(label)
        signature = hashlib.sha256(json.dumps([str(source), self.auth_store(label), self.auth(label)], sort_keys=True).encode()).hexdigest()
        if checked.get('fingerprint') != signature or checked.get('label') != label:
            checked = self.verify(label, force=True)
        if checked.get('cli_state') not in ('ready', 'api_key') or checked.get('reusable') is False:
            raise RuntimeError(checked.get('cli_detail') or 'Verify this Codex login first.')
        self.prepare_runtime()
        auth = source/'auth.json'
        if not auth.is_file():
            raise RuntimeError('No reusable Codex file login. Sign in through this profile first.')
        index = self.index()
        before = {name: os.readlink(self.shared/name) if (self.shared/name).is_symlink() else None
                  for name in ('auth.json', 'auth.lock')}
        for name in before:
            path = self.shared/name
            if path.exists() and not path.is_symlink():
                raise RuntimeError('The managed runtime has an independent login. Save it before switching accounts.')
        atomic_json(self.journal, {'before': before, 'active': index.get('active'),
                                  'active_identity': index.get('active_identity')})
        try:
            self._link('auth.json', auth.resolve())
            lock = source/'auth.lock'
            lock.touch(mode=0o600, exist_ok=True)
            self._link('auth.lock', lock.resolve())
            index['active'] = label
            index['active_identity'] = {k: checked.get(k) for k in ('account', 'user', 'email')}
            atomic_json(self.index_path, index)
            self.journal.unlink()
        except BaseException:
            self._recover()
            raise

    def activate(self, label, verified=None):
        with self.lease(exclusive=True), self.locked():
            if self.desktop_state().get('busy'):
                raise RuntimeError('CODEX_BUSY: Codex Desktop is using this history home. Account switching is deferred.')
            self._activate(label, verified)

    def _recover(self):
        journal = read_json(self.journal)
        if not journal:
            return
        before = journal.get('before')
        if not isinstance(before, dict) or set(before) != {'auth.json', 'auth.lock'} or any(
                target is not None and not isinstance(target, str) for target in before.values()):
            raise RuntimeError('The Codex recovery record is invalid. No files were changed.')
        for name, target in journal['before'].items():
            if target is None:
                (self.shared/name).unlink(missing_ok=True)
            else:
                self._link(name, target)
        index = self.index()
        index['active'] = journal.get('active')
        index['active_identity'] = journal.get('active_identity')
        atomic_json(self.index_path, index)
        self.journal.unlink()

    def recover(self):
        with self.lease(exclusive=True), self.locked():
            if self.desktop_state().get('busy'):
                raise RuntimeError('Close managed Codex Desktop before recovery.')
            self._recover()

    def remove(self, label):
        with self.lease(exclusive=True), self.locked():
            profile = self.profile(label)
            index = self.index()
            if index.get('active') == label:
                if self.desktop_state().get('busy'):
                    raise RuntimeError('Close managed Codex Desktop before removing its active profile.')
                for name in ('auth.json', 'auth.lock'):
                    path = self.shared/name
                    if path.is_symlink():
                        path.unlink()
                index.update(active=None, active_identity=None)
            index.setdefault('removed', {})[label] = {'profile': profile, 'removed_at': time.time()}
            index['profiles'].pop(label, None)
            atomic_json(self.index_path, index)

    def restore(self, label):
        validate(label)
        with self.locked():
            index = self.index()
            value = index.setdefault('removed', {}).pop(label, None)
            if not value:
                raise RuntimeError('This profile is not removed.')
            index['profiles'][label] = value['profile']
            atomic_json(self.index_path, index)

    def change_login(self, label):
        with self.lease(exclusive=True), self.locked():
            if self.index().get('active') == label and self.desktop_state().get('busy'):
                raise RuntimeError('Close managed Codex Desktop before changing this login.')
            old = self.profile(label)
            index = self.index()
            folder = self.store/'profiles'/f'{label}-{time.time_ns()}'
            private_dir(folder.parent)
            private_dir(folder)
            (folder/'config.toml').write_text('cli_auth_credentials_store = "file"\n')
            index.setdefault('previous_logins', []).append({'label': label, **old})
            index['profiles'][label] = {'source': str(folder), 'created_at': time.time()}
            if index.get('active') == label:
                for name in ('auth.json', 'auth.lock'):
                    if (self.shared/name).is_symlink():
                        (self.shared/name).unlink()
                index.update(active=None, active_identity=None)
            atomic_json(self.index_path, index)
            (self.store/'usage'/f'{label}.json').unlink(missing_ok=True)

    def login_source(self, label):
        """Get a managed sign-in home, preserving the original native login."""
        if self.profile(label).get('native') or self.auth_store(label) != 'file':
            self.change_login(label)
        return self.source(label)

    def _runtime_matches(self, label):
        return (self.index().get('active') == label and (self.shared/'auth.json').is_symlink()
                and (self.shared/'auth.json').resolve() == (self.source(label)/'auth.json').resolve())

    def launch_cli(self, label, extra=None, login=False):
        executable = codex_install.detect_cli(self.home)
        if not executable:
            raise RuntimeError('Install Codex CLI or Desktop first.')
        self.ensure_cli_supported(executable)
        if login:
            source = self.login_source(label)
            with self.lease(exclusive=True), self.locked():
                if self.index().get('active') == label and self.desktop_state().get('busy'):
                    raise RuntimeError('Close managed Codex Desktop before changing this login.')
                private_dir(source)
                code = subprocess.call([str(executable), '--no-daemon', '-c', 'cli_auth_credentials_store="file"', 'login'], env=environment(source))
                if code == 0:
                    (self.store/'usage'/f'{label}.json').unlink(missing_ok=True)
                    self.verify(label, force=True)
            return code
        # An already active account supports multiple concurrent CLI sessions.
        # Its shared lease protects the identity check through child exit.
        with self.lease():
            with self.locked():
                matched = self._runtime_matches(label)
            if matched:
                checked = self.verify(label)
                if checked.get('cli_state') not in ('ready', 'api_key') or checked.get('reusable') is False:
                    raise RuntimeError(checked.get('cli_detail') or 'Verify this Codex login first.')
                return subprocess.call([str(executable), '--no-daemon', *(extra or [])], env=environment(self.shared))
        with self.lease(exclusive=True) as held:
            with self.locked():
                if self.desktop_state().get('busy'):
                    raise RuntimeError('Close managed Codex Desktop before using a different CLI account.')
                self._activate(label)
            # Downgrade this same lock descriptor; never expose an unlocked
            # window between installing credentials and starting the child.
            fcntl.flock(held, fcntl.LOCK_SH)
            return subprocess.call([str(executable), '--no-daemon', *(extra or [])], env=environment(self.shared))

    def sync_history(self):
        import codex_history
        with self.lease(exclusive=True), self.locked():
            if self.desktop_state().get('busy'):
                raise RuntimeError('Close managed Codex Desktop before importing history.')
            self.prepare_runtime()
            return codex_history.import_history(self.native, self.shared)

    def switch_desktop(self, label):
        import codex_desktop
        app = codex_install.detect_desktop(self.home)
        if not app:
            raise RuntimeError('Install Codex Desktop first.')
        codex_desktop.ensure_supported(app)
        verified = self.verify(label)
        if verified.get('cli_state') not in ('ready', 'api_key'):
            raise RuntimeError(verified.get('cli_detail') or 'Sign in first.')
        # Explicit TUI action only. Rotation never quits Desktop automatically.
        with self.lease(exclusive=True), self.locked():
            codex_desktop.quit_app(self.shared, self.user_data)
            self._activate(label, verified)
            codex_desktop.launch_app(self.shared, self.user_data)

    def save_current(self, label):
        validate(label)
        with self.lease(exclusive=True), self.locked():
            index = self.index()
            active = index.get('active')
            if active in self.profiles() and self._runtime_matches(active):
                source = self.profile(active)
            else:
                source = {'source': str(self.native), 'native': True}
            if label in self.profiles():
                index.setdefault('previous_logins', []).append({'label': label, **self.profile(label)})
            if index.get('active') == label and self.profile(label).get('source') != source.get('source'):
                if self.desktop_state().get('busy'):
                    raise RuntimeError('Close managed Codex Desktop before changing its active profile association.')
                for name in ('auth.json', 'auth.lock'):
                    if (self.shared/name).is_symlink():
                        (self.shared/name).unlink()
                index.update(active=None, active_identity=None)
            index['profiles'][label] = dict(source)
            atomic_json(self.index_path, index)
            (self.store/'usage'/f'{label}.json').unlink(missing_ok=True)
        return self.verify(label, force=True)

    def status(self):
        index = self.index()
        desktop = self.desktop_state()
        rows = []
        for label in self.profiles():
            try:
                row = self.verify(label)
            except (OSError, ValueError, RuntimeError):
                row = {'label': label, 'cli_state': 'unverified', 'cli_detail': 'Could not check this login. Retry or sign in.'}
            row = {k: v for k, v in row.items() if k != 'fingerprint'}
            row.update(desktop_saved=row.get('cli_state') in ('ready', 'api_key'),
                       desktop_active=label == index.get('active'), cli_email=row.get('email'))
            rows.append(row)
        active = next((r for r in rows if r['label'] == index.get('active')), {})
        return {'provider': 'codex', 'accounts': rows, 'removed_profiles': list(index.get('removed', {})),
                'desktop_running': desktop.get('running', False), 'desktop_background': desktop.get('background', False),
                'desktop_managed_running': desktop.get('running', False),
                'desktop_other_running': bool(desktop.get('any_running') and not desktop.get('running')),
                'active_email': active.get('email'), 'active': index.get('active'),
                'recovery_pending': self.journal.exists(), 'cli_available': bool(codex_install.detect_cli(self.home)),
                'desktop_available': bool(codex_install.detect_desktop(self.home)), 'shared_dir': str(self.shared),
                'checked_at': time.time()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    options = parser.add_mutually_exclusive_group(required=True)
    for name in ('status-json', 'sync', 'recover'):
        options.add_argument('--'+name, action='store_true')
    for name in ('create', 'verify', 'save', 'switch', 'remove', 'restore', 'change-login', 'launch-cli', 'login'):
        options.add_argument('--'+name, metavar='LABEL')
    parser.add_argument('extra', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    manager = Manager()
    try:
        if args.status_json:
            print(json.dumps(manager.status()))
        elif args.launch_cli or args.login:
            extra = args.extra[1:] if args.extra[:1] == ['--'] else args.extra
            return manager.launch_cli(args.launch_cli or args.login, extra, login=bool(args.login))
        elif args.sync:
            print(json.dumps(manager.sync_history()))
        elif args.recover:
            manager.recover(); print('Codex switch recovered.')
        else:
            for action, method in (('create', manager.create), ('verify', lambda label: manager.verify(label, force=True)),
                                   ('save', manager.save_current), ('switch', manager.switch_desktop),
                                   ('remove', manager.remove), ('restore', manager.restore), ('change_login', manager.change_login)):
                label = getattr(args, action)
                if label:
                    result = method(label)
                    if action in ('verify', 'save') and result.get('cli_state') not in ('ready', 'api_key'):
                        raise RuntimeError(result.get('cli_detail') or 'Sign-in required.')
                    print('Codex profile updated. Local threads and history are retained.')
                    break
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'Codex action failed. No secrets were included in this error.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    import runtime
    runtime.run_main(main)
