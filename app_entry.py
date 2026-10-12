"""Standalone executable entrypoint. No external Python is used by child helpers."""
import runpy
from pathlib import Path
import subprocess
import sys
import runtime


def main():
    runtime.configure()
    if sys.argv[1:2] == ['--install']:
        import binary_install
        return binary_install.main(sys.argv[2:])
    if sys.argv[1:2] == ['--desktop-helper']:
        sys.argv.pop(1)
        runpy.run_path(str(Path(__file__).with_name('desktop-sync.py')), run_name='__main__')
        return 0
    if sys.argv[1:2] == ['--onboarding-helper']:
        sys.argv.pop(1)
        import onboarding
        try:
            return onboarding.main() or 0
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f'Setup stopped: {error}', file=sys.stderr)
            return 1
    import accounts_tui
    return accounts_tui.main()


if __name__ == '__main__':
    sys.exit(main())
