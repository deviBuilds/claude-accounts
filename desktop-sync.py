"""Sync local Desktop Code session entries using server-verified account IDs."""

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import desktop_profiles


def read_credentials(config_dir):
    service = "Claude Code-credentials"
    if config_dir is not None:
        service += "-" + hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Cannot read this profile's Keychain login; open Claude and sign in first.")
    raw = result.stdout.strip()
    try:
        try:
            credentials = json.loads(raw)
        except ValueError:
            credentials = json.loads(bytes.fromhex(raw).decode())
        token = credentials["claudeAiOauth"]["accessToken"]
        if not isinstance(token, str) or not token:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Unrecognized Claude Keychain credential format.") from None
    return credentials['claudeAiOauth']


def identity(config_dir):
    credentials = read_credentials(config_dir)
    expires = credentials.get('expiresAt')
    if isinstance(expires, (int, float)) and expires <= time.time() * 1000:
        command = 'claude' if config_dir is None else 'claude-' + config_dir.name.removeprefix('.claude-acct-')
        raise RuntimeError(f'CLI access token expired. Open {command}, then /exit to return and retry. '
                           'The interactive account manager has an Open CLI / refresh login action.')
    token = credentials['accessToken']
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/profile",
        headers={"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            profile = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"Profile lookup returned HTTP {error.code}; open this Claude profile to refresh its login, then retry."
        ) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError("Could not verify the account with Anthropic; retry when connected.") from None
    try:
        account = str(uuid.UUID(profile["account"]["uuid"]))
        organization = str(uuid.UUID(profile["organization"]["uuid"]))
        email = profile["account"]["email"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Anthropic returned an unrecognized profile; no files copied.") from None
    return account, organization, email


def copy_plan(directories):
    newest = {}
    for directory in directories:
        for source in sorted(directory.glob("local_*.json")):
            if source.is_symlink() or not source.is_file():
                continue
            previous = newest.get(source.name)
            if previous is None or source.stat().st_mtime_ns > previous.stat().st_mtime_ns:
                newest[source.name] = source
    plan = []
    for directory in directories:
        for name, source in sorted(newest.items()):
            destination = directory / name
            if destination.is_symlink():
                raise RuntimeError(f"Session entry is a symlink: {destination}; no files copied.")
            if not destination.exists() or source.stat().st_mtime_ns > destination.stat().st_mtime_ns:
                plan.append((source, destination))
    return plan


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''First-time Desktop setup (do not log out between these steps):
  claude-desktop-sync --save a     # save your current Desktop login
  claude-desktop-sync --login b    # opens a clean Desktop profile
  # Sign into the account for profile b in Desktop once.
  claude-desktop-sync --save b

Then: --switch b / --switch a. These commands quit and reopen Desktop.
--dry-run previews without changing files or quitting the app.
With no options, only local Code session entries are synchronized.
Snapshots stay in ~/.claude-desktop-profiles with private directory permissions.
Desktop cookies retain their Keychain encryption. Expired/revoked logins need sign-in.
--recover rolls back an interrupted switch. Old snapshots are retained.''')
    parser.add_argument("--dry-run", action="store_true", help="verify accounts and preview copies")
    parser.add_argument('--tui', action='store_true', help='open the interactive account manager')
    parser.add_argument('--status-json', action='store_true', help='read account health as JSON, without changing profiles')
    desktop_profiles.add_arguments(parser)
    args = parser.parse_args()
    if args.tui or args.status_json:
        if any([args.save, args.switch, args.login, args.list, args.recover, args.dry_run]) or (args.tui and args.status_json):
            parser.error('--tui and --status-json must be used alone')
        if args.tui:
            import accounts_tui
            return accounts_tui.main([])
        import account_status
        import account_usage
        print(json.dumps(account_status.collect(Path.home(), read_credentials, identity, account_usage.load)))
        return
    if desktop_profiles.handle(args, identity, copy_plan):
        return
    manager = desktop_profiles.Profiles(Path.home(), identity, copy_plan)
    with contextlib.nullcontext() if args.dry_run else manager.locked():
        manager.check()
        sync_sessions(args)


def sync_sessions(args):
    home = Path.home()
    root = home / "Library/Application Support/Claude/claude-code-sessions"
    if not root.is_dir():
        raise RuntimeError("No Desktop sessions directory.")
    profiles = [("a", None)] + [
        (p.name.removeprefix(".claude-acct-"), p)
        for p in sorted(home.glob(".claude-acct-*")) if p.is_dir()
    ]
    directories = []
    for name, config_dir in profiles:
        try:
            account, organization, email = identity(config_dir)
        except RuntimeError as error:
            raise RuntimeError(f"Account {name}: {error}") from None
        directory = root / account / organization
        print(f"{name}: {email} — {len(list(directory.glob('local_*.json')))} sessions", flush=True)
        if directory not in directories:
            directories.append(directory)
    plan = copy_plan(directories)
    print(f"{'Would copy' if args.dry_run else 'To copy'}: {len(plan)} session entries")
    if args.dry_run or not plan:
        return
    backup = Path(tempfile.mkdtemp(prefix=".claude-desktop-sessions-backup-", dir=home))
    # A failed backup aborts before any destination is changed.
    shutil.copytree(root, backup, dirs_exist_ok=True, symlinks=True)
    print(f"Backup: {backup}", flush=True)
    for source, destination in plan:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Atomically replace each entry so Desktop cannot read a partial JSON file.
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".sync-", delete=False) as temp:
            temporary = Path(temp.name)
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    for directory in directories:
        print(f"{len(list(directory.glob('local_*.json')))} sessions: {directory.relative_to(root)}")
    print("Reopen Claude Desktop to reload its Code session list.")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Sync stopped: {error}", file=sys.stderr)
        sys.exit(1)
