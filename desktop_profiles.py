"""Local Claude Desktop profile snapshots. No tokens are exported or replaced."""

import base64
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import uuid

APP = '/Applications/Claude.app'
BUNDLE = 'com.anthropic.claudefordesktop'
# These are disposable Electron caches, not account databases or session history.
CACHES = {'Cache', 'Code Cache', 'GPUCache', 'DawnGraphiteCache',
          'DawnWebGPUCache', 'Crashpad', 'SingletonLock', 'SingletonCookie', 'SingletonSocket'}


def clone_copy(source, destination):
    """APFS copies share unchanged blocks; other filesystems use ordinary copies."""
    try:
        clone = ctypes.CDLL('/usr/lib/libSystem.B.dylib').clonefile
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clone.restype = ctypes.c_int
        if clone(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return destination
    except (OSError, AttributeError):
        pass
    return shutil.copy2(source, destination)


def add_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--save', metavar='ACCOUNT', help='save the current Desktop login as a/b/c')
    group.add_argument('--switch', metavar='ACCOUNT', help='restore a saved Desktop login and reopen the app')
    group.add_argument('--login', metavar='ACCOUNT', help='preserve the current login and open a clean profile for one-time sign-in')
    group.add_argument('--list', action='store_true', help='list saved Desktop profiles')
    group.add_argument('--recover', action='store_true', help='roll back an interrupted profile switch')


def atomic_json(path, data):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temp.open('x') as stream:
            os.chmod(temp, 0o600)
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def safe_key():
    result = subprocess.run(
        ['security', 'find-generic-password', '-s', 'Claude Safe Storage', '-w'],
        capture_output=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError('Cannot read Claude Safe Storage from your login Keychain.')
    return hashlib.pbkdf2_hmac('sha1', result.stdout.rstrip(b'\n'), b'saltysalt', 1003, 16)


def decrypt_cookie(value, key, host):
    if not value.startswith(b'v10'):
        raise RuntimeError('Unsupported Desktop cookie encryption; no profile changed.')
    # CommonCrypto is part of macOS; no extra Python packages or decrypted files.
    decrypt = ctypes.CDLL('/usr/lib/libSystem.B.dylib').CCCrypt
    decrypt.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    decrypt.restype = ctypes.c_int32
    output = ctypes.create_string_buffer(len(value))
    count = ctypes.c_size_t()
    status = decrypt(1, 0, 1, key, len(key), b' ' * 16, value[3:], len(value) - 3,
                     output, len(value), ctypes.byref(count))
    if status:
        raise RuntimeError('Cannot decrypt Desktop login with the current Keychain.')
    raw = output.raw[:count.value]
    if raw[:32] == hashlib.sha256(host.encode()).digest():
        raw = raw[32:]
    try:
        return raw.decode()
    except UnicodeError:
        raise RuntimeError('Unrecognized Desktop cookie format.') from None


def desktop_account(root):
    cookie_path = root / 'Cookies'
    if not cookie_path.exists():
        return None
    with contextlib.closing(sqlite3.connect(cookie_path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute("SELECT host_key,encrypted_value,expires_utc,is_persistent FROM cookies "
                          "WHERE name='sessionKey' AND host_key IN ('.claude.ai','claude.ai')").fetchall()
    if not rows:
        return None
    key = safe_key()
    accounts = set()
    for host, encrypted, expiry, persistent in rows:
        if persistent and expiry / 1e6 - 11644473600 <= time.time():
            raise RuntimeError('The saved Desktop login has expired; sign in again.')
        token = decrypt_cookie(encrypted, key, host)
        if not token.startswith('sk-ant-sid02-'):
            raise RuntimeError('Cannot verify this Desktop session cookie format; no profile changed.')
        try:
            accounts.add(str(uuid.UUID(bytes=base64.urlsafe_b64decode(token[13:35] + '=='))))
        except (ValueError, TypeError):
            raise RuntimeError('Invalid account identifier in Desktop cookie.') from None
    config = json.loads((root / 'config.json').read_text())
    if len(accounts) != 1 or config.get('lastKnownAccountUuid') not in accounts:
        raise RuntimeError('Desktop cookie and account label disagree. Reopen Desktop and retry.')
    return next(iter(accounts))


def app_running():
    result = subprocess.run(['pgrep', '-f', '^/Applications/Claude[.]app/Contents/MacOS/Claude( |$)'],
                            capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('Cannot determine whether Claude Desktop is running.')
    return result.returncode == 0


def desktop_processes():
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,comm='], capture_output=True, text=True, check=True)
    records = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3:
            records.append((int(fields[0]), int(fields[1]), fields[2]))
    runtime = str(Path.home() / 'Library/Application Support/Claude/claude-code') + '/'
    owned = {pid for pid, _, command in records if command.startswith((APP + '/', runtime))}
    while True:
        children = {pid for pid, parent, _ in records if parent in owned}
        expanded = owned | children
        if expanded == owned:
            return owned
        owned = expanded


def quit_app():
    owned = desktop_processes()
    if app_running():
        print('Quitting Claude Desktop gracefully; this pauses its running sessions.', flush=True)
        result = subprocess.run(['osascript', '-e', f'tell application id "{BUNDLE}" to quit'],
                                capture_output=True, timeout=35)
        if result.returncode:
            raise RuntimeError('Claude did not quit. Quit it with Cmd+Q, then retry.')
    for _ in range(100):
        live = set()
        for pid in owned:
            try:
                os.kill(pid, 0)
                live.add(pid)
            except ProcessLookupError:
                pass
        if not live and not desktop_processes():
            return
        time.sleep(0.1)
    raise RuntimeError('Desktop or its background tasks are still running; no profile files changed. Stop them and retry.')


def open_app():
    result = subprocess.run(['open', '-a', APP], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError('Claude Desktop could not open. Install it in /Applications, then open it manually.')


class Profiles:
    def __init__(self, home, identity, copy_plan):
        self.home = home
        self.root = home / 'Library/Application Support/Claude'
        self.store = home / '.claude-desktop-profiles'
        self.index_path = self.store / 'index.json'
        self.journal = self.store / 'pending-switch.json'
        self.identity = identity
        self.copy_plan = copy_plan

    def index(self):
        return json.loads(self.index_path.read_text()) if self.index_path.exists() else {'profiles': {}}

    def snapshot_path(self, snapshot):
        if not re.fullmatch(r'[0-9a-f]{32}', snapshot):
            raise RuntimeError('Invalid saved snapshot identifier.')
        return self.store / 'snapshots' / snapshot

    @contextlib.contextmanager
    def locked(self):
        if self.store.is_symlink():
            raise RuntimeError('The Desktop profile store must not be a symlink.')
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.store, 0o700)
        with (self.store / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another Desktop profile operation is running.') from None
            yield

    def check(self):
        if self.journal.exists():
            raise RuntimeError('A switch was interrupted. Run claude-desktop-sync --recover first.')
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError('Open Claude Desktop once to initialize its data directory, then retry. The directory must not be a symlink.')

    def verified_label(self, name):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', name):
            raise RuntimeError('Account labels must use lowercase letters, digits, underscores or hyphens.')
        config = None if name == 'a' else self.home / ('.claude-acct-' + name)
        if config is not None and not config.is_dir():
            raise RuntimeError(f'No CLI profile {name!r} exists to verify this account.')
        account, organization, email = self.identity(config)
        return {'account': account, 'organization': organization, 'email': email}

    def snapshot(self, metadata):
        snapshot = uuid.uuid4().hex
        directory = self.snapshot_path(snapshot)
        directory.mkdir(parents=True, mode=0o700)
        # Keep all application state together; exclude only disposable caches.
        shutil.copytree(self.root, directory / 'data', symlinks=True, copy_function=clone_copy,
                        ignore=lambda path, names: CACHES.intersection(names) if Path(path) == self.root else [])
        atomic_json(directory / 'manifest.json', {**metadata, 'savedAt': time.time()})
        return snapshot

    def save(self, name, dry=False):
        self.check()
        expected = self.verified_label(name)
        if desktop_account(self.root) != expected['account']:
            raise RuntimeError(f'Desktop is not signed in as {expected["email"]}. Nothing saved under {name}.')
        index = self.index()
        existing = index['profiles'].get(name)
        if existing and existing['account'] != expected['account']:
            raise RuntimeError('This label already belongs to a different Desktop account.')
        if dry:
            print(f'Would save {expected["email"]} as {name}; Desktop would pause briefly and reopen.')
            return
        running = app_running()
        try:
            quit_app()
            if desktop_account(self.root) != expected['account']:
                raise RuntimeError('Desktop account changed while quitting; no profile saved.')
            snapshot = self.snapshot(expected)
            index['profiles'][name] = {**expected, 'snapshot': snapshot}
            index.pop('pendingLogin', None)
            atomic_json(self.index_path, index)
            print(f'Saved {name}: {expected["email"]}. Encrypted login and application state preserved.')
        finally:
            if running:
                open_app()

    def merge_code(self, target, index):
        relative = Path('claude-code-sessions')
        accounts = {(p['account'], p['organization']) for p in index['profiles'].values()}
        sources = [self.root / relative]
        sources += [self.snapshot_path(p['snapshot']) / 'data' / relative for p in index['profiles'].values()]
        # Retain the newest entries for unregistered accounts in their original folders too.
        for source_root in sources:
            for source in source_root.glob('*/*/local_*.json'):
                try:
                    uuid.UUID(source.parent.name)
                    uuid.UUID(source.parent.parent.name)
                except ValueError:
                    continue
                if not source.is_file() or source.is_symlink():
                    continue
                destination = target / relative / source.relative_to(source_root)
                if destination.is_symlink():
                    raise RuntimeError('Refusing to replace a symlink in saved Code history.')
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists() or source.stat().st_mtime_ns > destination.stat().st_mtime_ns:
                    shutil.copy2(source, destination)
        # Gather newest session entries across every saved profile without copying schedules.
        newest = {}
        for source in sources:
            for account, org in accounts:
                for entry in (source / account / org).glob('local_*.json'):
                    if entry.is_file() and not entry.is_symlink():
                        old = newest.get(entry.name)
                        if old is None or entry.stat().st_mtime_ns > old.stat().st_mtime_ns:
                            newest[entry.name] = entry
        for account, org in accounts:
            destination = target / relative / account / org
            destination.mkdir(parents=True, exist_ok=True)
            for name, source in newest.items():
                file = destination / name
                if file.is_symlink():
                    raise RuntimeError('Refusing to replace a symlink in saved Code history.')
                if not file.exists() or source.stat().st_mtime_ns > file.stat().st_mtime_ns:
                    shutil.copy2(source, file)

    def switch(self, name, login=False, dry=False):
        self.check()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', name):
            raise RuntimeError('Invalid account label.')
        index = self.index()
        selected = index['profiles'].get(name)
        if login:
            expected = self.verified_label(name)
            if selected:
                raise RuntimeError(f'{name} is already saved; use --switch {name}.')
        else:
            if not selected:
                raise RuntimeError(f'No saved Desktop profile {name}. Use --login {name}, sign in, then --save {name}.')
            expected = selected
            source = self.snapshot_path(selected['snapshot']) / 'data'
            if desktop_account(source) != selected['account']:
                raise RuntimeError('Saved Desktop account does not match its label.')
        current = desktop_account(self.root)
        if not login and current == expected['account']:
            print(f'Desktop is already using {expected["email"]}.')
            return
        outgoing = {label: p for label, p in index['profiles'].items() if current and p['account'] == current}
        if current and not outgoing:
            from account_status import discover
            for candidate in discover(self.home, index):
                # A saved label must never be silently reassigned to another account.
                if candidate in index['profiles']:
                    continue
                try:
                    known = self.verified_label(candidate)
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    continue
                if known['account'] == current:
                    outgoing[candidate] = known
                    break
            if not outgoing:
                raise RuntimeError('Current Desktop login has no verified CLI profile. Add or refresh its CLI account first, '
                                   'then retry. The current Desktop login has been kept intact.')
        if dry:
            print(f'Would save current Desktop account {list(outgoing) or ["unsigned profile"]}, '
                  f'{"open a clean login for" if login else "restore"} {name}: {expected["email"]}, and reopen Desktop.')
            return
        was_running = app_running()
        try:
            quit_app()
            if desktop_account(self.root) != current:
                raise RuntimeError('Desktop account changed while quitting; retry.')
            outgoing_meta = next(iter(outgoing.values())) if outgoing else {'account': None}
            snapshot = self.snapshot(outgoing_meta)
            updated = json.loads(json.dumps(index))
            for label, metadata in outgoing.items():
                updated['profiles'][label] = {**metadata, 'snapshot': snapshot}
            stage = self.store / ('stage-' + uuid.uuid4().hex)
            if login:
                stage.mkdir(mode=0o700)
                updated['pendingLogin'] = {'label': name, **expected}
            else:
                shutil.copytree(source, stage, symlinks=True, copy_function=clone_copy)
                self.merge_code(stage, updated)
                updated.pop('pendingLogin', None)
            # The original active directory stays intact as the immediate rollback copy.
            rollback = 'rollback-' + uuid.uuid4().hex
            atomic_json(self.journal, {'rollback': rollback, 'stage': stage.name, 'indexBefore': index})
            try:
                self.root.rename(self.store / rollback)
                stage.rename(self.root)
                atomic_json(self.index_path, updated)
                self.journal.unlink()
            except BaseException:
                self.recover(reopen=False)
                raise
            for label, metadata in outgoing.items():
                print(f'Saved current Desktop account: {label} — {metadata["email"]}.')
            print(f'Preserved outgoing profile. Recovery copy: {self.store / rollback}')
        except BaseException:
            if was_running and not app_running() and not self.journal.exists():
                open_app()
            raise
        open_app()
        if login:
            print(f'Sign into {expected["email"]} in Desktop, then run claude-desktop-sync --save {name}.')
        else:
            print(f'Restored {name}: {expected["email"]}. Open your conversation in the Code tab.')

    def recover(self, reopen=True):
        if not self.journal.exists():
            print('No interrupted switch to recover.')
            return
        transaction = json.loads(self.journal.read_text())
        rollback_name = transaction['rollback']
        if not re.fullmatch(r'rollback-[0-9a-f]{32}', rollback_name):
            raise RuntimeError('Invalid recovery journal.')
        rollback = self.store / rollback_name
        quit_app()
        if rollback.exists():
            if self.root.exists():
                self.root.rename(self.store / ('failed-' + uuid.uuid4().hex))
            rollback.rename(self.root)
        if not self.root.is_dir():
            raise RuntimeError('The original Desktop data could not be found; recovery stopped.')
        atomic_json(self.index_path, transaction['indexBefore'])
        self.journal.unlink()
        print('Recovered the original Desktop profile.')
        if reopen:
            open_app()


def handle(args, identity, copy_plan):
    if not any([args.save, args.switch, args.login, args.list, args.recover]):
        return False
    manager = Profiles(Path.home(), identity, copy_plan)
    if args.list:
        index = manager.index()
        for name, profile in sorted(index['profiles'].items()):
            print(f'{name}: {profile["email"]}')
        if not index['profiles']:
            print('No saved Desktop profiles. Open claude-accounts to connect your CLI and Desktop logins.')
        if index.get('pendingLogin'):
            print('Pending sign-in:', index['pendingLogin']['label'], index['pendingLogin']['email'])
        return True
    if args.recover and args.dry_run:
        print('Recovery pending.' if manager.journal.exists() else 'No interrupted switch.')
        return True
    # Dry runs are read-only, including the profile store itself.
    with contextlib.nullcontext() if args.dry_run else manager.locked():
        if args.save:
            manager.save(args.save, args.dry_run)
        elif args.switch or args.login:
            manager.switch(args.switch or args.login, login=bool(args.login), dry=args.dry_run)
        else:
            manager.recover()
    return True
