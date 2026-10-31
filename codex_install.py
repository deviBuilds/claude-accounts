"""Detect and install official Codex clients without touching account state."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess


BUNDLE_ID = 'com.openai.codex'
TEAM_ID = '2DC432GLL2'
APP = Path('/Applications/Codex.app')
# Official documentation: https://learn.chatgpt.com/docs/cli and /docs/app.
# The documented chatgpt.com/codex/install.sh redirects to this official URL.
CLI_INSTALLER = 'https://releases.openai.com/codex/install.sh'
DESKTOP_DOWNLOAD = 'https://persistent.oaistatic.com/codex-app-prod/Codex.dmg'
REQUIREMENT = ('=anchor apple generic and identifier "' + BUNDLE_ID +
               '" and certificate leaf[subject.OU] = "' + TEAM_ID + '"')


def bundle_identity(path):
    try:
        info = plistlib.loads((Path(path)/'Contents/Info.plist').read_bytes())
        return info.get('CFBundleIdentifier') if isinstance(info, dict) else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def detect_desktop(home=None, roots=None):
    """Return the installed Codex Desktop bundle, including its ChatGPT name."""
    home = Path.home() if home is None else Path(home)
    roots = roots if roots is not None else [home/'Applications', Path('/Applications')]
    for root in roots:
        root = Path(root)
        known = [root/'Codex.app', root/'ChatGPT.app']
        for path in known + sorted(p for p in root.glob('*.app') if p not in known):
            if path.is_dir() and bundle_identity(path) == BUNDLE_ID:
                return path
    return None


def bundled_cli(app):
    """Find a CLI in either the native-package or older Desktop layout."""
    resources = Path(app)/'Contents/Resources'
    for name in ('codex-cli/bin/codex', 'codex-cli/CodexCLI.app/Contents/MacOS/codex',
                 'codex-cli', 'codex'):
        path = resources/name
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def detect_cli(home=None, which=None, include_bundled=True, desktop=None):
    """Return an executable CLI path; Desktop's bundled CLI is also usable."""
    which = shutil.which if which is None else which
    visible = which('codex')
    home = Path.home() if home is None else Path(home)
    candidates = ([Path(visible)] if visible else []) + [home/'.local/bin/codex']
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    if include_bundled:
        app = desktop if desktop is not None else detect_desktop(home)
        if app:
            return bundled_cli(app)
    return None


def download(url, target):
    # Lazy import keeps dependency detection usable from installer preferences.
    from binary_install import download as fetch
    return fetch(url, target)


def install_cli(work, home=None, runner=subprocess.run, fetch=download, which=None):
    """Reuse a native CLI or install the checksum-verified official package."""
    home = Path.home() if home is None else Path(home)
    executable = detect_cli(home, which=which, include_bundled=False)
    if executable:
        runner([str(executable), '--version'], check=True)
        print('  Codex CLI: already installed', flush=True)
        return executable
    target = home/'.local/bin/codex'
    if target.exists() or target.is_symlink():
        raise RuntimeError(f'Cannot install Codex over {target}; move the conflicting file first.')
    print('  Installing Codex using the official standalone installer…', flush=True)
    script = Path(work)/'codex-install.sh'
    fetch(CLI_INSTALLER, script)
    env = {**os.environ, 'CODEX_NON_INTERACTIVE': '1', 'CODEX_INSTALL_DIR': str(target.parent)}
    # The official installer verifies release checksums and installs all native
    # helpers. A single bare CLI binary is insufficient for current releases.
    runner(['/bin/sh', str(script)], env=env, check=True)
    executable = detect_cli(home, which=which, include_bundled=False)
    if not executable:
        raise RuntimeError('Codex installation did not produce a usable CLI. Fix the installer error and retry.')
    runner(['/usr/bin/codesign', '--verify', '--strict', '-R',
            '=anchor apple generic and certificate leaf[subject.OU] = "' + TEAM_ID + '"',
            str(executable.resolve())], check=True)
    runner([str(executable), '--version'], check=True)
    return executable


def install_desktop(work, target=APP, home=None, runner=subprocess.run, fetch=download):
    """Install a signed Desktop bundle; never replace an existing installation."""
    installed = detect_desktop(home)
    if installed:
        print('  Codex Desktop: already installed', flush=True)
        return installed
    target = Path(target)
    if target.exists() or target.is_symlink():
        raise RuntimeError(f'Cannot install Codex Desktop over {target}; move the conflicting file first.')
    work = Path(work)
    image = work/'Codex.dmg'
    mount = work/'codex-mount'
    mount.mkdir()
    print('  Downloading Codex Desktop…', flush=True)
    fetch(DESKTOP_DOWNLOAD, image)
    runner(['/usr/bin/hdiutil', 'attach', str(image), '-readonly', '-nobrowse',
            '-mountpoint', str(mount)], check=True, capture_output=True)
    try:
        apps = [p for p in mount.glob('*.app')
                if not p.is_symlink() and p.is_dir() and bundle_identity(p) == BUNDLE_ID]
        if len(apps) != 1:
            raise RuntimeError('Codex Desktop image layout changed; no application was installed.')
        app = apps[0]
        runner(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', REQUIREMENT, str(app)], check=True)
        if target.exists() or target.is_symlink() or detect_desktop(home):
            raise RuntimeError('Codex Desktop appeared during installation. Rerun to reuse it.')
        command = ['/usr/bin/ditto', str(app), str(target)]
        if not os.access(target.parent, os.W_OK):
            print('  macOS needs administrator permission to place Codex in /Applications.', flush=True)
            command.insert(0, '/usr/bin/sudo')
        runner(command, check=True)
        if bundle_identity(target) != BUNDLE_ID:
            raise RuntimeError('Codex Desktop installation did not create the expected application.')
        return target
    finally:
        runner(['/usr/bin/hdiutil', 'detach', str(mount)], check=False, capture_output=True)
