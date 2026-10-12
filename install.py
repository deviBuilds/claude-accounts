#!/usr/bin/env python3
"""Install this source bundle for the current macOS user, without shell edits."""
import os
from pathlib import Path
import shlex
import shutil
import sys

FILES = ('accounts_tui.py', 'account_status.py', 'account_usage.py', 'cli_refresh.py',
         'desktop-sync.py', 'desktop_profiles.py', 'onboarding.py', 'runtime.py', 'setup_options.py')


def install(source, home, python):
    destination = home / '.local/lib/claude-accounts'
    binaries = home / '.local/bin'
    destination.mkdir(parents=True, exist_ok=True)
    binaries.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        src, target = source/name, destination/name
        if src.resolve() != target.resolve():
            temporary = target.with_suffix('.install-tmp')
            shutil.copyfile(src, temporary)
            os.chmod(temporary, 0o644)
            temporary.replace(target)
    for command, module in [('claude-accounts', 'accounts_tui.py'), ('claude-desktop-sync', 'desktop-sync.py')]:
        launcher = binaries / command
        # Never replace someone else's symlink target.
        if launcher.is_symlink():
            raise RuntimeError(f'Refusing to replace a symlink: {launcher}')
        launcher.write_text('#!/bin/sh\nexec '+shlex.quote(str(python))+' '+shlex.quote(str(destination/module))+' "$@"\n')
        launcher.chmod(0o755)
    return binaries/'claude-accounts'


def main():
    if sys.platform != 'darwin':
        sys.exit('This release supports macOS only (Desktop and Keychain integration).')
    if sys.version_info < (3, 10):
        sys.exit('Python 3.10 or newer is required.')
    command = install(Path(__file__).resolve().parent, Path.home(), Path(sys.executable).resolve())
    print(f'Installed. Run: {command}')
    if not shutil.which('claude'):
        print('Install Claude Code first: https://code.claude.com/docs/en/setup')
    if not Path('/Applications/Claude.app').is_dir():
        print('Install Claude Desktop in /Applications: https://claude.ai/download')
    if str(command.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print('Optional: add ~/.local/bin to PATH to run claude-accounts by name.')
    print('No credentials, Desktop profiles, or shell configuration were installed or replaced.')


if __name__ == '__main__':
    main()
