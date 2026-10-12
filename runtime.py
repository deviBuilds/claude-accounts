"""Command routing shared by source and self-contained releases."""
import os
from pathlib import Path
import sys


def command(script, *args):
    if getattr(sys, 'frozen', False):
        mode = {'desktop-sync.py': '--desktop-helper', 'onboarding.py': '--onboarding-helper'}[Path(script).name]
        return [sys.executable, mode, *args]
    return [sys.executable, str(script), *args]


def configure():
    # Finder/Terminal launches need to find the native CLI without shell edits.
    candidates = [str(Path.home()/'.local/bin'), '/opt/homebrew/bin', '/usr/local/bin']
    os.environ['PATH'] = os.pathsep.join(dict.fromkeys(candidates + os.environ.get('PATH', '/usr/bin:/bin').split(os.pathsep)))
    if getattr(sys, 'frozen', False):
        import certifi
        os.environ.setdefault('SSL_CERT_FILE', certifi.where())
