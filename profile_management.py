"""Explicit, reversible profile associations. Never moves OAuth credentials."""
import hashlib
import json
import time
from pathlib import Path

import desktop_profiles as dp


def remember(index, label, profile):
    if profile:
        history = index.setdefault('bindingHistory', [])
        if not any(p.get('snapshot') == profile.get('snapshot') for p in history):
            history.append({**profile, 'previousLabel': label, 'archivedAt': time.time()})


def candidates(index):
    return list(index.get('profiles', {}).values()) + index.get('bindingHistory', [])


def repair_plan(index, identities):
    """Plan all available rebindings together, so swapped profiles repair atomically."""
    changes = []
    for label, identity in sorted(identities.items()):
        old = index.get('profiles', {}).get(label)
        if not old or old.get('account') == identity['account']:
            continue
        matches = [p for p in candidates(index) if p.get('account') == identity['account']]
        if matches:
            source = matches[0]
            changes.append({'label': label, 'before': old, 'after': {**source, **identity}})
    signature = hashlib.sha256(json.dumps({'index': index, 'identities': identities}, sort_keys=True).encode()).hexdigest()
    return {'changes': changes, 'signature': signature}


def verified_identities(manager):
    from account_status import discover
    identities = {}
    for label in discover(manager.home, manager.index()):
        try:
            identities[label] = manager.verified_label(label)
        except (RuntimeError, OSError):
            pass
    return identities


def reconcile(manager, label, expected):
    index = manager.index()
    if index.get('pendingLogin'):
        raise RuntimeError('Finish or cancel the pending Desktop sign-in before reconciling profiles.')
    plan = repair_plan(index, verified_identities(manager))
    if not expected or plan['signature'] != expected:
        raise RuntimeError('Account state changed since the preview. Refresh accounts and review the repair again.')
    if not any(c['label'] == label for c in plan['changes']):
        raise RuntimeError('No matching saved Desktop login is available for this CLI account. Change its Desktop login instead.')
    # Validate every selected encrypted snapshot before changing any association.
    for change in plan['changes']:
        after = change['after']
        if dp.desktop_account(manager.snapshot_path(after['snapshot']) / 'data') != after['account']:
            raise RuntimeError('A saved Desktop login could not be verified. No associations changed.')
    for change in plan['changes']:
        remember(index, change['label'], change['before'])
        index['profiles'][change['label']] = change['after']
    dp.atomic_json(manager.index_path, index)
    for change in plan['changes']:
        print(f"Matched {change['label']}: {change['after'].get('email') or change['after']['account']}.")
    print('CLI logins and the running Desktop app were not changed. Previous associations are retained.')


def remove(manager, label):
    from account_status import discover
    index = manager.index()
    if label not in discover(manager.home, index):
        raise RuntimeError('This profile is no longer in the manager.')
    if index.get('pendingLogin', {}).get('label') == label:
        raise RuntimeError('Finish or cancel this profile’s Desktop sign-in before removing it.')
    # Soft removal is intentional: shared CLI directories and Keychain logins
    # must not be deleted just to remove an entry from the manager.
    profile = index['profiles'].pop(label, None)
    index.setdefault('removedProfiles', {})[label] = {'profile': profile, 'removedAt': time.time()}
    dp.atomic_json(manager.index_path, index)
    print(f'Removed {label} from the manager. Logins and local history are retained.')
    print('Restore it from Tools → Removed profiles. The label stays reserved until restored.')


def restore(manager, label):
    index = manager.index()
    removed = index.get('removedProfiles', {}).get(label)
    if removed is None:
        raise RuntimeError('This profile is not removed.')
    if label in index['profiles']:
        raise RuntimeError('This label is already in use; restore stopped.')
    if removed.get('profile'):
        index['profiles'][label] = removed['profile']
    del index['removedProfiles'][label]
    dp.atomic_json(manager.index_path, index)
    print(f'Restored {label}. Refresh its logins to check for identity changes.')


def reset_cli_setup(manager, label):
    index = manager.index()
    if label in index.get('removedProfiles', {}):
        raise RuntimeError('Restore this profile before changing its login.')
    if index.get('pendingLogin', {}).get('label') == label:
        raise RuntimeError('Finish or cancel Desktop sign-in before changing this CLI login.')
    (manager.store / f'onboarding-{label}.json').unlink(missing_ok=True)
    print('Previous setup identity cleared. Choose the intended account in the official CLI sign-in flow.')


def begin_desktop_change(manager, label, cli_target=False):
    index = manager.index()
    if label in index.get('removedProfiles', {}):
        raise RuntimeError('Restore this profile before changing its login.')
    pending = index.get('pendingLogin')
    if pending:
        if pending.get('label') != label or not pending.get('replaceDesktop'):
            raise RuntimeError('Finish the pending Desktop sign-in first.')
        dp.open_app()
        return
    expected = manager.verified_label(label) if cli_target else {'account': None, 'organization': None, 'email': None}
    manager.switch(label, login=True, login_identity=expected, replace_desktop=True)
    print('Sign into the new account in Desktop, then choose Save new Desktop login.')


def finish_desktop_change(manager, label):
    manager.check()
    index = manager.index()
    pending = index.get('pendingLogin', {})
    if pending.get('label') != label or not pending.get('replaceDesktop'):
        raise RuntimeError('Start Change Desktop login before replacing a saved login.')
    running, stopped = dp.app_running(), False
    try:
        dp.quit_app()
        stopped = True
        account = dp.desktop_account(manager.root)
        if not account:
            raise RuntimeError('Desktop is not signed in yet. Complete sign-in, then retry saving.')
        if pending.get('account') and pending['account'] != account:
            raise RuntimeError('Desktop does not match the CLI account selected for this change. Sign into the intended account and retry.')
        metadata = {'account': account, 'organization': None, 'email': None, 'desktopOnly': True}
        for known in candidates(index) + list(verified_identities(manager).values()):
            if known.get('account') == account:
                metadata.update({k: known.get(k) for k in ('account', 'organization', 'email')})
        snapshot = manager.snapshot(metadata)
        remember(index, label, index['profiles'].get(label))
        index['profiles'][label] = {**metadata, 'snapshot': snapshot}
        index.pop('pendingLogin', None)
        dp.atomic_json(manager.index_path, index)
        (manager.store / f'onboarding-{label}.json').unlink(missing_ok=True)
        print('New Desktop login saved. The CLI login was not changed; review any identity mismatch.')
    finally:
        if running and stopped:
            dp.open_app()


def cancel_desktop_change(manager, label):
    index = manager.index()
    pending = index.get('pendingLogin', {})
    if pending.get('label') != label:
        raise RuntimeError('This profile has no pending Desktop sign-in.')
    index.pop('pendingLogin', None)
    dp.atomic_json(manager.index_path, index)
    (manager.store / f'onboarding-{label}.json').unlink(missing_ok=True)
    print('Sign-in setup cancelled. Saved logins and the current Desktop app are unchanged.')


def handle(manager, action, label, expected=None):
    if manager.journal.exists():
        raise RuntimeError('Recover the interrupted Desktop switch before managing profiles.')
    actions = {'remove': remove, 'restore': restore, 'reset-cli-setup': reset_cli_setup,
               'begin-desktop-change': begin_desktop_change, 'finish-desktop-change': finish_desktop_change,
               'cancel-desktop-change': cancel_desktop_change}
    if action == 'desktop-to-cli':
        return begin_desktop_change(manager, label, cli_target=True)
    if action == 'reconcile':
        return reconcile(manager, label, expected)
    return actions[action](manager, label)
