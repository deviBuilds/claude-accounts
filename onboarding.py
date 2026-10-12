"""Resumable account setup. Authentication stays in the official Claude clients."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import desktop_profiles as dp

SHARED = ('settings.json', 'skills', 'plugins', 'projects', 'history.jsonl', 'plans',
          'file-history', 'tasks', 'agents', 'commands', 'ide', 'chrome', 'downloads',
          'CLAUDE.md', 'keybindings.json')
PREFERENCES = ('projects', 'hasCompletedOnboarding', 'lastOnboardingVersion',
               'installMethod', 'autoUpdates', 'autoUpdatesProtectedForNative')


def validate(label):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', label):
        raise RuntimeError('Use 1–32 lowercase letters, numbers, hyphens or underscores.')


def progress_path(manager, label):
    validate(label)
    return manager.store / ('onboarding-' + label + '.json')


def create(manager, label):
    validate(label)
    index = manager.index()
    if label == 'a' or label in index['profiles'] or index.get('pendingLogin', {}).get('label') == label:
        raise RuntimeError('That label is already in use. Select the existing account to resume setup.')
    target = manager.home / ('.claude-acct-' + label)
    # mkdir without exist_ok also rejects dangling symlinks and concurrent creation.
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise RuntimeError('That CLI profile already exists. Select it to resume setup.') from None
    try:
        source = manager.home / '.claude'
        source.mkdir(mode=0o700, exist_ok=True)
        for name in ('skills', 'plugins', 'projects', 'plans', 'file-history', 'tasks', 'agents', 'commands', 'ide', 'chrome', 'downloads'):
            (source / name).mkdir(mode=0o700, exist_ok=True)
        for name in SHARED:
            (target / name).symlink_to(source / name)
        original = manager.home / '.claude.json'
        settings = json.loads(original.read_text()) if original.exists() else {}
        dp.atomic_json(target / '.claude.json', {k: settings[k] for k in PREFERENCES if k in settings})
        dp.atomic_json(progress_path(manager, label), {'label': label, 'phase': 'cli'})
    except BaseException:
        # Only our newly created directory is removed; existing profiles are never reused.
        shutil.rmtree(target)
        raise
    print(f'Created CLI profile {label}. Sign in to continue.')


def pin_identity(manager, label):
    expected = manager.verified_label(label)
    path = progress_path(manager, label)
    progress = json.loads(path.read_text()) if path.exists() else {}
    pending = manager.index().get('pendingLogin', {})
    pinned = progress.get('account') or (pending.get('account') if pending.get('label') == label else None)
    if pinned and expected['account'] != pinned:
        raise RuntimeError('The CLI account changed during setup. Sign back into the original CLI account before continuing.')
    dp.atomic_json(path, {'label': label, 'phase': 'desktop', **expected})
    return expected


def prepare(manager, label):
    expected = pin_identity(manager, label)
    if not manager.root.exists() and not manager.root.is_symlink() and not manager.journal.exists():
        dp.open_app()
        print(f'First Desktop launch: sign into {expected["email"]}, then choose Verify and finish.')
        return
    manager.check()
    index = manager.index()
    pending = index.get('pendingLogin', {})
    if pending and pending['label'] != label:
        raise RuntimeError(f'Finish Desktop setup for {pending["label"]} first (select it in the account menu).')
    current = dp.desktop_account(manager.root)
    if current == expected['account']:
        dp.open_app()
        print(f'Desktop already matches {expected["email"]}. Choose Verify and finish to save it.')
        return
    if pending:
        # Resuming must not discard a partially completed sign-in.
        dp.open_app()
        print(f'Resumed Desktop setup. Sign into {expected["email"]}, then choose Verify and finish.')
        return
    if label in index['profiles']:
        if index['profiles'][label]['account'] != expected['account']:
            raise RuntimeError('The CLI login differs from the saved Desktop account for this label.')
        manager.switch(label)
        return
    # Switching saves the outgoing account in the same transaction, including
    # an account that has never had a Desktop snapshot before.
    manager.switch(label, login=True)


def finish(manager, label):
    manager.check()
    expected = pin_identity(manager, label)
    pending = manager.index().get('pendingLogin', {})
    if pending and pending['label'] != label:
        raise RuntimeError(f'Finish Desktop setup for {pending["label"]} first.')
    # Recheck the pinned identity even if credentials change between these calls.
    original = manager.identity
    target = None if label == 'a' else manager.home / ('.claude-acct-' + label)
    def checked(config):
        result = original(config)
        if config == target and result[0] != expected['account']:
            raise RuntimeError('CLI identity changed; nothing saved.')
        return result
    running = dp.app_running()
    manager.identity = checked
    try:
        # Chromium can keep a newly acquired cookie in memory. Quitting flushes
        # its session store; only the resulting cookie is evidence for saving.
        print('Checking the new Desktop login. Closing Desktop gracefully to save its sign-in state.', flush=True)
        dp.quit_app()
        if dp.desktop_account(manager.root) != expected['account']:
            raise RuntimeError(f'Desktop must be signed into {expected["email"]}. Finish sign-in in Desktop, then retry.')
        manager.save(label)
    finally:
        manager.identity = original
        if running:
            dp.open_app()
    progress_path(manager, label).unlink(missing_ok=True)
    command = 'claude' if label == 'a' else 'claude-' + label
    print(f'Setup complete: {expected["email"]}. CLI and Desktop identities match.')
    print(f'CLI: claude-accounts --cli {label}. Desktop: select this account and Switch.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'prepare', 'finish'])
    parser.add_argument('label')
    args = parser.parse_args()
    validate(args.label)
    spec = importlib.util.spec_from_file_location('desktop_sync', Path(__file__).with_name('desktop-sync.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    manager = dp.Profiles(Path.home(), helper.identity, helper.copy_plan)
    with manager.locked():
        if manager.journal.exists():
            raise RuntimeError('Recover the interrupted Desktop switch from Tools before continuing setup.')
        {'create': create, 'prepare': prepare, 'finish': finish}[args.action](manager, args.label)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'Setup stopped: {error}', file=sys.stderr)
        sys.exit(1)
