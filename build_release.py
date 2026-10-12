"""Build the standalone release on the target Mac. Build dependencies only."""
import argparse
import base64
import hashlib
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

import install


def installer_script(binary, architecture):
    digest = hashlib.sha256(binary).hexdigest()
    payload = base64.encodebytes(binary).decode('ascii')
    return f'''#!/bin/bash
set -euo pipefail
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != {architecture} ]]; then
  echo "This installer is for macOS {architecture}. Download the release for your Mac." >&2
  exit 1
fi
if [[ "$(sw_vers -productVersion | cut -d. -f1)" -lt 13 ]]; then
  echo "macOS 13 or newer is required." >&2
  exit 1
fi
workdir=$(mktemp -d "${{TMPDIR:-/tmp}}/claude-accounts.XXXXXX")
trap 'rm -rf "$workdir"' EXIT
/usr/bin/base64 -D > "$workdir/claude-accounts" <<'CLAUDE_ACCOUNTS_BINARY'
{payload}CLAUDE_ACCOUNTS_BINARY
actual=$(/usr/bin/shasum -a 256 "$workdir/claude-accounts")
if [[ "${{actual%% *}}" != {digest} ]]; then
  echo "Installer checksum mismatch. Download a fresh copy." >&2
  exit 1
fi
chmod 755 "$workdir/claude-accounts"
"$workdir/claude-accounts" --install "$@"
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path.home()/'.local/share/claude-accounts/releases')
    parser.add_argument('--work', type=Path, default=Path.home()/'.cache/claude-accounts-build/pyinstaller')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build on macOS for the target architecture.')
    source = Path(__file__).resolve().parent
    architecture = platform.machine()
    args.output.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--console',
                    '--name', 'claude-accounts', '--target-architecture', architecture,
                    '--distpath', str(args.work/'dist'), '--workpath', str(args.work/'build'),
                    '--specpath', str(args.work), '--paths', str(source),
                    '--add-data', str(source/'desktop-sync.py')+':.',
                    '--hidden-import', 'account_status', '--collect-data', 'certifi',
                    str(source/'app_entry.py')], check=True)
    binary = (args.work/'dist/claude-accounts').read_bytes()
    filename = f'claude-accounts-macos-{architecture}'
    target = args.output/filename
    target.write_bytes(binary)
    target.chmod(0o755)
    script = args.output/(filename+'-install.command')
    script.write_text(installer_script(binary, architecture))
    script.chmod(0o755)
    files = sorted(set(install.FILES) | {'install.py', 'README.md', 'app_entry.py', 'binary_install.py',
                                      'build_release.py', 'requirements-build.txt', 'install.sh'} | {p.name for p in source.glob('test_*.py')})
    with zipfile.ZipFile(args.output/'claude-accounts-macos.zip', 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in files:
            bundle.write(source/name, 'claude-accounts/'+name)
    manifest = args.output/(filename+'.sha256')
    manifest.write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in (target,script)))
    print(f'Executable: {target}\nSingle-command installer: {script}')


if __name__ == '__main__':
    main()
