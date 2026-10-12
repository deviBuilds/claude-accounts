"""Read-only status for the TUI. Never returns tokens or trusts CLI account labels."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import time

import desktop_profiles as dp
import setup_options
from cli_refresh import credential_id


def discover(home, index):
    labels = {'a'} | set(index.get('profiles', {}))
    labels |= {p.name.removeprefix('.claude-acct-') for p in home.glob('.claude-acct-*') if p.is_dir()}
    labels |= {p.stem.removeprefix('onboarding-') for p in (home / '.claude-desktop-profiles').glob('onboarding-*.json')}
    if index.get('pendingLogin'):
        labels.add(index['pendingLogin']['label'])
    return sorted(label for label in labels if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', label))


def collect(home, read_credentials, identity, usage_loader=None):
    manager = dp.Profiles(home, identity, None)
    errors = []
    components = setup_options.load(home)
    try:
        index = manager.index()
    except (ValueError, OSError) as error:
        index = {'profiles': {}}
        errors.append(f'Cannot read saved profile index: {error}')
    try:
        active = dp.desktop_account(manager.root) if 'desktop' in components else None
        desktop_detail = 'Desktop not selected' if 'desktop' not in components else 'Local login verified' if active else 'Not signed in'
    except (RuntimeError, OSError, ValueError) as error:
        active, desktop_detail = None, str(error)
    try:
        running = dp.app_running() if 'desktop' in components else None
    except (RuntimeError, OSError):
        running = None

    def inspect(label):
        saved = index.get('profiles', {}).get(label, {})
        pending = index.get('pendingLogin', {})
        known = saved or (pending if pending.get('label') == label else {})
        row = {'label': label, 'email': known.get('email'), 'email_source': 'saved Desktop profile' if saved else 'pending sign-in',
               'desktop_saved': bool(saved), 'desktop_active': bool(active and known.get('account') == active),
               'pending': pending.get('label') == label, 'cli_state': 'unverified', 'cli_detail': '', 'expires_at': None}
        if usage_loader:
            row['usage'] = usage_loader(home, known.get('account'))
        if 'cli' not in components:
            row.update(cli_state='not_selected', cli_detail='CLI is not selected. Usage percentages require a CLI login.')
            row['usage'] = {'state': 'unavailable', 'windows': [], 'detail': 'Usage needs a CLI login.'}
            return row
        config = None if label == 'a' else home / ('.claude-acct-' + label)
        try:
            credential = read_credentials(config)
            row['refresh_id'] = credential_id(credential)
            expires = credential.get('expiresAt')
            row['expires_at'] = expires / 1000 if isinstance(expires, (float, int)) else None
            if row['expires_at'] is not None and row['expires_at'] <= time.time():
                row.update(cli_state='expired', cli_detail='Access token expired; the account manager will try background renewal.')
                return row
        except (RuntimeError, OSError) as error:
            row.update(cli_state='missing', cli_detail=str(error))
            return row
        try:
            account, _, email = identity(config)
            row.update(email=email, email_source='verified with Anthropic', cli_state='ready',
                       cli_detail='Account verified with Anthropic.', desktop_active=account == active)
            if usage_loader:
                row['usage'] = usage_loader(home, account, credential)
            if saved and saved.get('account') != account:
                row['cli_detail'] = 'CLI account differs from the saved Desktop profile: ' + str(saved.get('email'))
        except (RuntimeError, OSError) as error:
            row.update(cli_state='unverified', cli_detail=str(error))
        return row

    with ThreadPoolExecutor(max_workers=4) as pool:
        labels = discover(home, index)
        if 'cli' not in components and index['profiles'] and 'a' not in index['profiles'] and not (manager.store/'onboarding-a.json').exists():
            labels = [label for label in labels if label != 'a']
        accounts = list(pool.map(inspect, labels))
    first_run = len(accounts) == 1 and accounts[0]['label'] == 'a' and accounts[0]['cli_state'] in ('missing', 'not_selected') and not index.get('profiles')
    return {'accounts': accounts, 'desktop_running': running, 'desktop_detail': desktop_detail,
            'first_run': first_run, 'cli_available': bool(shutil.which('claude')), 'desktop_installed': Path(dp.APP).is_dir(),
            'active_account': active, 'pending_login': index.get('pendingLogin'),
            'recovery_pending': manager.journal.exists(), 'errors': errors, 'checked_at': time.time()}
