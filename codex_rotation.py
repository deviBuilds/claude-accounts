"""Five-minute Codex rotation, deferred while the shared history home is busy."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from codex_accounts import Manager, INTERVAL, private_dir, read_json
from desktop_profiles import atomic_json
import runtime

AGENT = 'ai.claude-accounts.codex-rotation'


def public_usage(usage):
    """Keep native credential material and internal cache metadata out of status."""
    result = {key: usage[key] for key in ('state', 'detail', 'checked_at', 'next_check') if key in usage}
    result['windows'] = [
        {key: window[key] for key in ('key', 'label', 'percent', 'resets_at', 'window_minutes') if key in window}
        for window in usage.get('windows', []) if isinstance(window, dict)]
    return result


class Rotation:
    def __init__(self, home=None, manager=None):
        self.manager = manager or Manager(home)
        self.home = self.manager.home
        self.control = self.home/'.codex-account-rotation'
        self.shared = self.manager.shared

    @contextlib.contextmanager
    def locked(self):
        private_dir(self.control)
        path = self.control/'rotation.lock'
        if path.is_symlink():
            raise RuntimeError('Unsafe Codex rotation lock.')
        with path.open('a') as stream:
            path.chmod(0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def config(self):
        value = read_json(self.control/'config.json', {'enabled': False, 'accounts': []})
        if not isinstance(value, dict) or not isinstance(value.get('accounts'), list):
            raise RuntimeError('Codex rotation configuration is invalid.')
        return value

    def status(self):
        config = self.config()
        value = read_json(self.control/'status.json', {})
        if not isinstance(value, dict):
            raise RuntimeError('Codex rotation status is invalid.')
        return {**value, 'enabled': config.get('enabled', False), 'accounts': config['accounts'],
                'poll_seconds': INTERVAL, 'shared_dir': str(self.shared),
                'stale': time.time()-value.get('checked_at', 0) > 3*INTERVAL}

    def tick(self):
        with self.locked():
            config = self.config()
            if not config.get('enabled'):
                return self.status()
            rows, summary = {}, []
            available = self.manager.profiles()
            for saved in config['accounts']:
                label = saved['label']
                try:
                    row = self.manager.verify(label) if label in available else {}
                    if label not in available:
                        state, detail = 'removed', 'This profile was removed. Update the rotation selection.'
                    elif row.get('cli_state') == 'unverified' and row.get('error_kind') in ('unavailable', 'rate_limited'):
                        state, detail = 'unknown', row.get('cli_detail') or 'Quota check unavailable. Retrying at the next poll.'
                    elif row.get('cli_state') == 'unverified' and row.get('error_kind') == 'limited':
                        state, detail = 'limited', 'A reported Codex usage limit is exhausted.'
                    elif row.get('cli_state') in ('missing', 'sign_in_required', 'api_key', 'external') or row.get('reusable') is False:
                        state, detail = 'auth', row.get('cli_detail', 'Sign in through a managed Codex profile again.')
                    elif row.get('account') and (row.get('account'), row.get('user')) != (saved.get('account'), saved.get('user')):
                        state, detail = 'changed', 'This profile now uses another account. Reselect it to authorize rotation.'
                    elif row.get('cli_state') != 'ready' or row.get('usage', {}).get('state') != 'ready':
                        state, detail = 'unknown', row.get('cli_detail') or row.get('usage', {}).get('detail') or 'Quota check unavailable.'
                    elif row.get('limited'):
                        state, detail = 'limited', 'A reported Codex usage limit is exhausted.'
                    else:
                        state, detail = 'ready', 'Login and quotas verified.'
                    row = {**row, 'state': state, 'detail': detail}
                except (OSError, ValueError, RuntimeError):
                    row = {'state': 'unknown', 'detail': 'Could not check this profile. Retrying at the next poll.'}
                rows[label] = row
                summary.append({'label': label, **{k: row[k] for k in ('state', 'detail', 'email') if k in row},
                                **({'usage': public_usage(row['usage'])} if isinstance(row.get('usage'), dict) else {})})
            previous = read_json(self.control/'status.json', {})
            current = self.manager.index().get('active') or previous.get('active')
            if current not in available:
                current = None
            ready = [p['label'] for p in config['accounts'] if rows[p['label']]['state'] == 'ready']
            target = current if current in ready else None
            hold = current in rows and rows[current]['state'] == 'unknown'
            state, detail = 'ready', 'Ready. New Codex sessions use the selected account.'
            pending = None
            if hold:
                state, detail = 'unknown', rows[current]['detail']+' Keeping the current login.'
                target = current
            elif not target and ready:
                order = [p['label'] for p in config['accounts']]
                offset = (order.index(current)+1) % len(order) if current in order else 0
                target = next(label for label in order[offset:]+order[:offset] if label in ready)
            if not target:
                state, detail = 'waiting', 'No verified account with quota. Waiting for reset or sign-in.'
            elif not hold and (target != self.manager.index().get('active') or not (self.shared/'auth.json').is_symlink()):
                try:
                    self.manager.activate(target, verified=rows[target])
                except RuntimeError as error:
                    if 'CODEX_BUSY:' not in str(error):
                        raise
                    pending = target
                    target = current
                    state, detail = 'waiting_idle', 'Account change queued. Codex keeps credentials in memory even after a reply. Close the running CLI/app-server or managed Desktop, then resume the same thread.'
            now = time.time()
            result = {'active': target or current, 'pending': pending, 'state': state, 'detail': detail,
                      'checked_at': now, 'profiles': summary,
                      'switched_at': now if target and target != current else previous.get('switched_at')}
            atomic_json(self.control/'status.json', result)
            return self.status()

    def agent_path(self):
        return self.home/'Library/LaunchAgents'/f'{AGENT}.plist'

    def enable(self, labels):
        labels = list(dict.fromkeys(labels))
        if len(labels) < 2:
            raise RuntimeError('Select at least two different verified ChatGPT accounts.')
        accounts, seen = [], set()
        for label in labels:
            row = self.manager.verify(label)
            if (row.get('cli_state') != 'ready' or row.get('usage', {}).get('state') != 'ready'
                    or row.get('reusable') is False or row.get('kind', 'chatgpt') != 'chatgpt'):
                raise RuntimeError('Verify each selected Codex login and its quota before enabling rotation.')
            key = row.get('account'), row.get('user')
            if not key[0] or key in seen:
                raise RuntimeError('Select distinct Codex accounts. Duplicate logins do not add quota.')
            seen.add(key)
            accounts.append({'label': label, 'account': key[0], 'user': key[1], 'email': row.get('email')})
        path = self.agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RuntimeError('Unsafe Codex rotation service path.')
        domain = f'gui/{os.getuid()}'
        with self.locked():
            old = self.config()
            atomic_json(self.control/'config.json', {'enabled': True, 'accounts': accounts})
            spec = {'Label': AGENT, 'ProgramArguments': runtime.command(Path(__file__), '--tick'),
                    'RunAtLoad': True, 'StartInterval': INTERVAL, 'ProcessType': 'Background',
                    'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
                    'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
            subprocess.run(['launchctl', 'bootout', domain+'/'+AGENT], capture_output=True)
            path.write_bytes(plistlib.dumps(spec)); path.chmod(0o600)
            result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
            if result.returncode:
                atomic_json(self.control/'config.json', {**old, 'enabled': False})
                path.unlink(missing_ok=True)
                raise RuntimeError('Could not start Codex rotation. Retry from a logged-in macOS desktop.')

    def disable(self):
        with self.locked():
            atomic_json(self.control/'config.json', {**self.config(), 'enabled': False})
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{AGENT}'], capture_output=True)
        self.agent_path().unlink(missing_ok=True)

    def launch(self, extra):
        if extra and extra[0] in ('login', 'logout'):
            raise RuntimeError('Manage logins through Codex Accounts. The shared home retains one canonical account login.')
        if not self.config().get('enabled'):
            raise RuntimeError('Enable Codex auto rotation in Tools first.')
        status = self.tick()
        if status.get('state') not in ('ready', 'unknown') or not status.get('active'):
            raise RuntimeError(status.get('detail') or 'No Codex account is ready.')
        return self.manager.launch_cli(status['active'], extra)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    rotation = Rotation()
    try:
        if args[:1] == ['--run']:
            return rotation.launch(args[1:])
        parser = argparse.ArgumentParser(description=__doc__)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--enable', nargs='+', metavar='PROFILE')
        for action in ('disable', 'tick', 'status'):
            group.add_argument('--'+action, action='store_true')
        options = parser.parse_args(args)
        if options.enable:
            rotation.enable(options.enable)
            print('Codex rotation enabled. Checks run every 5 minutes; account changes wait until the managed runtime is idle.')
        elif options.disable:
            rotation.disable(); print('Codex rotation disabled. Logins and local history are retained.')
        elif options.tick:
            print(json.dumps(rotation.tick()))
        else:
            print(json.dumps(rotation.status()))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        detail = str(error) if isinstance(error, RuntimeError) else 'Codex rotation check failed. Retry from Tools.'
        if args == ['--tick']:
            with rotation.locked():
                previous = read_json(rotation.control/'status.json', {})
                atomic_json(rotation.control/'status.json', {**previous, 'state': 'error', 'detail': detail, 'checked_at': time.time()})
        print(detail, file=sys.stderr)
        return 1


if __name__ == '__main__':
    runtime.run_main(main)
