"""Dependency checks and installation for the standalone macOS executable."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

APP = Path('/Applications/Claude.app')
CASK = 'https://formulae.brew.sh/api/cask/claude.json'
CLI_INSTALLER = 'https://claude.ai/install.sh'
# Checked against the installed, signed official Claude Desktop application.
REQUIREMENT = '=anchor apple generic and identifier "com.anthropic.claudefordesktop" and certificate leaf[subject.OU] = "Q6L2SF6YDW"'


def download(url, target):
    request = urllib.request.Request(url, headers={'User-Agent': 'ClaudeAccountsInstaller/1'})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response, target.open('wb') as output:
        if urllib.parse.urlparse(response.url).scheme != 'https':
            raise RuntimeError('Download redirected to an insecure connection.')
        while data := response.read(1024*1024):
            digest.update(data)
            output.write(data)
    return digest.hexdigest()


def desktop_metadata(value):
    url, checksum = value.get('url', ''), value.get('sha256', '')
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'downloads.claude.ai' or not parsed.path.endswith('.zip'):
        raise RuntimeError('Desktop download format changed; install Desktop from https://claude.com/download and rerun.')
    if not re.fullmatch('[a-f0-9]{64}', checksum):
        raise RuntimeError('Desktop download has no valid checksum.')
    return url, checksum


def install_desktop(work, target=APP, runner=subprocess.run, fetch=download):
    if target.is_dir():
        print('  Claude Desktop: already installed', flush=True)
        return
    if target.exists() or target.is_symlink():
        raise RuntimeError(f'Cannot install over {target}; move the conflicting file first.')
    metadata = work/'desktop.json'
    fetch(CASK, metadata)
    url, checksum = desktop_metadata(json.loads(metadata.read_text()))
    print('  Downloading Claude Desktop…', flush=True)
    archive = work/'Claude.zip'
    if fetch(url, archive) != checksum:
        raise RuntimeError('Desktop checksum mismatch. Nothing was installed; retry the installer.')
    # Check paths before asking ditto to preserve the signed app bundle/symlinks.
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            path = Path(name)
            if path.is_absolute() or '..' in path.parts:
                raise RuntimeError('Unsafe path in Desktop archive.')
    extracted = work/'desktop'
    runner(['/usr/bin/ditto', '-x', '-k', str(archive), str(extracted)], check=True)
    app = extracted/'Claude.app'
    if not app.is_dir():
        raise RuntimeError('Desktop archive layout changed; no application was installed.')
    runner(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', REQUIREMENT, str(app)], check=True)
    if target.exists():
        raise RuntimeError('Desktop appeared during installation. Rerun to use the existing app.')
    command = ['/usr/bin/ditto', str(app), str(target)]
    if not os.access(target.parent, os.W_OK):
        print('  macOS needs administrator permission to place Claude in /Applications.', flush=True)
        command.insert(0, '/usr/bin/sudo')
    runner(command, check=True)
    if not target.is_dir():
        raise RuntimeError('Desktop installation did not create the application.')


def install_cli(work, runner=subprocess.run, fetch=download, which=shutil.which):
    executable = which('claude')
    if executable:
        # A failed installed CLI is reported, never silently overwritten.
        runner([executable, '--version'], check=True)
        print('  Claude Code: already installed', flush=True)
        return
    print('  Installing Claude Code using the official native installer…', flush=True)
    script = work/'claude-install.sh'
    fetch(CLI_INSTALLER, script)
    runner(['/bin/bash', str(script)], check=True)
    executable = which('claude')
    if not executable:
        raise RuntimeError('Claude Code installation did not produce a usable command. Fix the installer error and rerun.')
    runner([executable, '--version'], check=True)


def install_binary(source, home):
    binaries = home/'.local/bin'
    binaries.mkdir(parents=True, exist_ok=True)
    target = binaries/'claude-accounts'
    helper = binaries/'claude-desktop-sync'
    for path in (target, helper):
        if path.is_symlink() or path.is_dir():
            raise RuntimeError(f'Refusing to replace a symlink or directory: {path}')
    with tempfile.TemporaryDirectory(prefix='.claude-accounts-install-', dir=binaries) as directory:
        staging = Path(directory)
        if source.resolve() != target.resolve():
            copied = staging/'claude-accounts'
            shutil.copyfile(source, copied)
            copied.chmod(0o755)
            copied.replace(target)
        launcher = staging/'claude-desktop-sync'
        launcher.write_text('#!/bin/sh\nexec '+shlex.quote(str(target))+' --desktop-helper "$@"\n')
        launcher.chmod(0o755)
        launcher.replace(helper)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='report dependencies without installing or opening anything')
    parser.add_argument('--no-launch', action='store_true', help='install dependencies and app without opening the TUI')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        parser.error('This installer supports macOS only.')
    if int(platform.mac_ver()[0].split('.')[0]) < 13:
        parser.error('macOS 13 or newer is required.')
    print('CLAUDE ACCOUNTS — standalone installation', flush=True)
    print('Python, Node.js and Homebrew are not required.', flush=True)
    if args.check:
        print('Claude Code: '+(shutil.which('claude') or 'will install'))
        print('Claude Desktop: '+('already installed' if APP.is_dir() else 'will install'))
        print(f'Account manager: {Path.home() / ".local/bin/claude-accounts"}')
        return 0
    if not getattr(sys, 'frozen', False):
        parser.error('Use the standalone release for automatic installation; use install.py for a source installation.')
    if not args.no_launch and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error('Run this installer in Terminal so onboarding can open, or use --no-launch.')
    if os.geteuid() == 0:
        parser.error('Run as your normal user, not with sudo. Only the Desktop copy may request administrator access.')
    try:
        with tempfile.TemporaryDirectory(prefix='claude-accounts-dependencies-') as directory:
            install_cli(Path(directory))
            install_desktop(Path(directory))
        target = install_binary(Path(sys.executable), Path.home())
        print(f'Installed: {target}', flush=True)
        if args.no_launch:
            return 0
        print('Opening account setup. Browser sign-in is completed by you.', flush=True)
        # Stay in this process: its bundled libraries and certificate store are alive.
        import accounts_tui
        return accounts_tui.main(['--onboard'])
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f'Installation stopped: {error}\nYou can rerun the installer after fixing this step.', file=sys.stderr)
        return 1
