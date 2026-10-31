"""Five-minute account rotation. Shared access tokens; canonical refresh tokens stay put."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import account_usage
import cli_refresh
import desktop_profiles as dp
import runtime

INTERVAL = 300
AGENT = 'ai.claude-accounts.rotation'
MARKER = 'claude-accounts-rotation-v1'


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def private_dir(path):
    if path.is_symlink():
        raise RuntimeError('Rotation directory must not be a symlink.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def safe_file(path):
    if path.is_symlink():
        raise RuntimeError('Rotation files must not be symlinks.')


class Backend:
    def __init__(self, home):
        self.home = home
        self.read_credentials, self.identity = cli_refresh.helpers()

    def available(self):
        from account_status import discover
        return discover(self.home, dp.Profiles(self.home, self.identity, None).index())

    def inspect(self, label, cache):
        config = None if label == 'a' else self.home / ('.claude-acct-' + label)
        try:
            credential = self.read_credentials(config)
        except RuntimeError:
            return {'state': 'auth', 'detail': 'CLI sign-in required.'}
        expires = credential.get('expiresAt')
        if not isinstance(expires, (int, float)):
            return {'state': 'auth', 'detail': 'CLI token expiry is unknown.'}
        if expires <= time.time()*1000:
            try:
                cli_refresh.refresh_account(label, self.home, self.read_credentials, self.identity)
                credential = self.read_credentials(config)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                return {'state': 'auth', 'detail': 'Expired CLI login could not be renewed.'}
            if credential.get('expiresAt', 0) <= time.time()*1000:
                return {'state': 'auth', 'detail': 'CLI renewal failed; sign in again.'}
        # Verify the exact token we will publish, not a separately re-read token.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request('https://api.anthropic.com/api/oauth/profile', headers={
            'Authorization': 'Bearer ' + credential['accessToken'], 'anthropic-beta': 'oauth-2025-04-20'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
                profile = json.load(response)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            return {'state': 'auth' if code == 401 else 'unknown', 'detail': f'Identity check unavailable (HTTP {code}).'}
        account, organization = profile['account']['uuid'], profile['organization']['uuid']
        usage = account_usage.load(cache, account, credential, ttl=INTERVAL)
        result = {'state': 'ready', 'account': account, 'organization': organization,
                  'email': profile['account']['email'], 'credential': credential, 'usage': usage}
        if usage['state'] != 'ready':
            result.update(state='auth' if 'HTTP 401' in usage.get('detail', '') else 'unknown', detail=usage.get('detail', 'Usage unknown.'))
        elif any(w['percent'] >= 100 for w in usage['windows']):
            # Conservative across model-specific limits until caller model is known.
            result.update(state='limited', detail='A reported usage window is exhausted.')
        return result


class Rotation:
    def __init__(self, home=None, backend=None):
        self.home = Path.home() if home is None else home
        self.control = self.home / '.claude-account-rotation'
        self.shared = self.home / '.claude-rotate'
        self.backend = backend

    @contextlib.contextmanager
    def locked(self):
        private_dir(self.control)
        path = self.control/'rotation.lock'
        safe_file(path)
        with path.open('a') as stream:
            os.chmod(path, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def config(self):
        return read_json(self.control/'config.json', {'enabled': False, 'accounts': []})

    def status(self):
        try:
            config = self.config()
            result = read_json(self.control/'status.json', {})
            if not isinstance(result, dict) or not isinstance(config.get('accounts'), list) or 'enabled' not in config:
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            return {'enabled': False, 'accounts': [], 'stale': True, 'detail': 'Rotation state cannot be read. Repair the rotation configuration before enabling.'}
        return {**result, 'enabled': config['enabled'], 'accounts': config['accounts'],
                'shared_dir': str(self.shared), 'poll_seconds': INTERVAL,
                'stale': time.time()-result.get('checked_at', 0) > 3*INTERVAL}

    def prepare(self):
        if self.shared.exists():
            if self.shared.is_symlink() or read_json(self.shared/'.rotation-managed', None) != MARKER:
                raise RuntimeError('The shared directory already exists and is not managed by rotation. Move it before enabling.')
        else:
            private_dir(self.shared)
            dp.atomic_json(self.shared/'.rotation-managed', MARKER)
        private_dir(self.shared)
        for name in ('.credentials.json', '.claude.json'):
            safe_file(self.shared/name)
        # A Keychain credential would take precedence over the managed file.
        service = 'Claude Code-credentials-' + hashlib.sha256(str(self.shared).encode()).hexdigest()[:8]
        result = subprocess.run(['security', 'find-generic-password', '-s', service], capture_output=True, timeout=20)
        if result.returncode == 0:
            raise RuntimeError('The rotation directory has its own Keychain login. Use an ordinary profile for sign-in; rotation cannot safely override it.')
        if result.returncode != 44:
            raise RuntimeError('Cannot verify that the rotation Keychain slot is empty.')

    def enroll(self, labels):
        labels = list(dict.fromkeys(labels))
        if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', x) for x in labels):
            raise RuntimeError('Select at least two signed-in CLI profiles.')
        backend = self.backend or Backend(self.home)
        available = backend.available()
        accounts, identities = [], set()
        for label in labels:
            if label not in available:
                raise RuntimeError('A selected profile is no longer available.')
            config = None if label == 'a' else self.home/('.claude-acct-'+label)
            account, organization, _ = backend.identity(config)
            if account in identities:
                raise RuntimeError('Selected profiles must belong to distinct accounts.')
            identities.add(account)
            accounts.append({'label': label, 'account': account, 'organization': organization})
        with self.locked():
            self.prepare()
            dp.atomic_json(self.control/'config.json', {'enabled': True, 'accounts': accounts})
        return accounts

    def publish(self, row):
        credential = row['credential']
        # Do not clone refresh credentials. Only the original CLI owns renewal.
        public = {k: credential[k] for k in ('accessToken', 'expiresAt', 'scopes', 'subscriptionType', 'rateLimitTier') if k in credential}
        config = read_json(self.shared/'.claude.json', {})
        config.update(hasCompletedOnboarding=True, oauthAccount={
            'accountUuid': row['account'], 'organizationUuid': row['organization'], 'emailAddress': row['email']})
        # Authentication is committed last with atomic rename. Never copy sessions/settings.
        dp.atomic_json(self.shared/'.claude.json', config)
        dp.atomic_json(self.shared/'.credentials.json', {'claudeAiOauth': public})

    def tick(self):
        with self.locked():
            config = self.config()
            if not config['enabled']:
                return self.status()
            self.prepare()
            backend = self.backend or Backend(self.home)
            available = backend.available()
            previous = read_json(self.control/'status.json', {})
            rows, summaries = {}, []
            for selected in config['accounts']:
                label = selected['label']
                try:
                    row = backend.inspect(label, self.control) if label in available else {'state': 'removed', 'detail': 'Profile removed.'}
                    if row.get('account') and (row['account'], row.get('organization')) != (selected['account'], selected['organization']):
                        row = {'state': 'changed', 'detail': 'CLI account changed; reselect profiles to authorize it.'}
                except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                    row = {'state': 'unknown', 'detail': 'Account check failed; retrying next poll.'}
                rows[label] = row
                summaries.append({'label': label, **{k: row[k] for k in ('state', 'detail', 'usage') if k in row}})
            current = previous.get('active')
            ready = [a['label'] for a in config['accounts'] if rows[a['label']]['state'] == 'ready']
            selected = current if current in ready else None
            # A transient error is not evidence that an account should rotate.
            hold = current in rows and rows[current]['state'] == 'unknown'
            if hold:
                selected = current
            elif not selected and ready:
                order = [a['label'] for a in config['accounts']]
                offset = (order.index(current)+1) % len(order) if current in order else 0
                selected = next(x for x in order[offset:]+order[:offset] if x in ready)
            now = time.time()
            state = {'active': selected or current, 'checked_at': now, 'profiles': summaries,
                     'state': 'unknown' if hold else 'ready' if selected else 'waiting',
                     'detail': 'Account check unavailable; retaining current credentials.' if hold else
                               'Access token synchronized. Existing processes use it on their next request.' if selected else
                               'No eligible account. Waiting for reset or sign-in; last access token is retained.',
                     'switched_at': now if selected and selected != current else previous.get('switched_at', now)}
            if selected and not hold:
                self.publish(rows[selected])
            dp.atomic_json(self.control/'status.json', state)
            return self.status()

    def agent_path(self):
        return self.home/'Library/LaunchAgents'/f'{AGENT}.plist'

    def enable(self, labels):
        old = self.config()
        self.enroll(labels)
        path = self.agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_file(path)
        args = runtime.command(Path(__file__), '--tick')
        spec = {'Label': AGENT, 'ProgramArguments': args, 'RunAtLoad': True, 'StartInterval': INTERVAL,
                'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
                'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null', 'ProcessType': 'Background'}
        domain = f'gui/{os.getuid()}'
        subprocess.run(['launchctl', 'bootout', domain+'/'+AGENT], capture_output=True)
        path.write_bytes(plistlib.dumps(spec));path.chmod(0o600)
        result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
        if result.returncode:
            with self.locked():
                dp.atomic_json(self.control/'config.json', {**old, 'enabled': False})
            path.unlink(missing_ok=True)
            raise RuntimeError('Could not start the rotation LaunchAgent. Rotation is disabled; retry from a logged-in macOS desktop.')

    def disable(self):
        with self.locked():
            config = self.config()
            dp.atomic_json(self.control/'config.json', {**config, 'enabled': False})
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{AGENT}'], capture_output=True)
        self.agent_path().unlink(missing_ok=True)

    def update_schedule(self):
        """Migrate an enabled service on upgrade without re-enrolling accounts."""
        path = self.agent_path()
        if not path.exists() or not self.config().get('enabled'):
            return False
        with self.locked():
            if not path.exists() or not self.config().get('enabled'):
                return False
            safe_file(path)
            original = path.read_bytes()
            spec = plistlib.loads(original)
            if spec.get('Label') != AGENT:
                raise RuntimeError('Unexpected rotation service label; schedule was not changed.')
            if spec.get('StartInterval') == INTERVAL:
                return False
            spec['StartInterval'] = INTERVAL
            domain = f'gui/{os.getuid()}'
            subprocess.run(['launchctl', 'bootout', domain+'/'+AGENT], capture_output=True)
            try:
                path.write_bytes(plistlib.dumps(spec))
                path.chmod(0o600)
                result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
                if result.returncode:
                    raise RuntimeError('Could not reload the rotation schedule. Retry installation or re-enable rotation.')
            except (OSError, RuntimeError):
                path.write_bytes(original)
                subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
                raise
            return True

    def launch(self, args):
        if args and (args[0] in ('login', 'logout', 'setup-token') or (args[0] == 'auth' and args[1:2] != ['status'])):
            raise RuntimeError('Sign in using an ordinary account profile, then select it in the rotation TUI.')
        state = self.status()
        if not state['enabled']:
            raise RuntimeError('Enable account rotation in Claude Accounts first.')
        if state['stale']:
            state = self.tick()
        if not state.get('active') or state.get('state') not in ('ready', 'unknown'):
            raise RuntimeError('No eligible rotation account. Check Auto rotation in Claude Accounts.')
        saved = read_json(self.shared/'.credentials.json', {}).get('claudeAiOauth', {})
        if saved.get('expiresAt', 0) <= time.time()*1000:
            state = self.tick()
            saved = read_json(self.shared/'.credentials.json', {}).get('claudeAiOauth', {})
            if saved.get('expiresAt', 0) <= time.time()*1000 or state['state'] != 'ready':
                raise RuntimeError('Rotation token expired and could not be renewed. Check the TUI.')
        executable = shutil.which('claude')
        if not executable:
            raise RuntimeError('Claude CLI was not found.')
        env = cli_refresh.environment('a', self.home)
        env.pop('CLAUDE_CODE_SAFE_MODE', None)
        env['CLAUDE_CONFIG_DIR'] = str(self.shared)
        os.execve(executable, [executable, *args], env)


def record_error(rotation, args, detail):
    if args != ['--tick']:
        return
    try:
        with rotation.locked():
            state = read_json(rotation.control/'status.json', {})
            dp.atomic_json(rotation.control/'status.json', {**state, 'state': 'error', 'detail': detail,
                                                          'checked_at': time.time()})
    except (OSError, ValueError, RuntimeError):
        pass


def main(argv=None):
    runtime.configure()
    args = sys.argv[1:] if argv is None else argv
    rotation = Rotation()
    try:
        if args[:1] == ['--run']:
            return rotation.launch(args[1:])
        parser = argparse.ArgumentParser(description=__doc__)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--enable', nargs='+', metavar='PROFILE')
        group.add_argument('--disable', action='store_true')
        group.add_argument('--tick', action='store_true')
        group.add_argument('--status', action='store_true')
        options = parser.parse_args(args)
        if options.enable:
            rotation.enable(options.enable)
            print('Rotation enabled. Background checks run every 5 minutes. Use claude-rotate, or set T3Code CLAUDE_CONFIG_DIR to ~/.claude-rotate.')
        elif options.disable:
            rotation.disable()
            print('Rotation disabled. Shared sessions and the last access token are retained. Existing CLI processes were not stopped.')
        elif options.tick:
            rotation.tick()
        else:
            print(json.dumps(rotation.status()))
        return 0
    except RuntimeError as error:
        record_error(rotation, args, str(error))
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        record_error(rotation, args, 'Rotation check failed; retry from Auto rotation in the TUI.')
        # Never print remote bodies or exceptions that could contain credentials.
        print('Rotation could not complete. Check selected CLI logins, the shared directory, and LaunchAgent status.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
