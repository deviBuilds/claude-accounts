"""Exercise the macOS /dev/tty -> native CLI handoff with real processes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

import runtime


@unittest.skipUnless(sys.platform == 'darwin', 'macOS kqueue and system script regression')
class TerminalHandoffTests(unittest.TestCase):
    def test_dev_tty_is_replaced_with_pollable_terminal_and_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix='cli-terminal-test-') as directory:
            root = Path(directory)
            child = root / 'native_probe.py'
            child.write_text(textwrap.dedent('''\
                import os, select, sys
                assert all(os.isatty(fd) for fd in (0, 1, 2))
                assert os.environ['CLAUDE_CONFIG_DIR'].endswith('/.claude-acct-test')
                assert sys.argv[1] == 'argument with spaces; not a shell command'
                queue = select.kqueue()
                queue.control([select.kevent(0, filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD)], 0, 0)
                queue.close()
                print('NATIVE_CLI_TERMINAL_READY', flush=True)
                sys.exit(7)
            '''))
            driver = root / 'driver.py'
            driver.write_text(textwrap.dedent('''\
                import errno, os, select, subprocess, sys
                import runtime
                with open('/dev/tty', 'rb', buffering=0) as terminal:
                    # Reproduce the exact descriptor that curl's installer passes on.
                    queue = select.kqueue()
                    try:
                        queue.control([select.kevent(terminal.fileno(), filter=select.KQ_FILTER_READ,
                                      flags=select.KQ_EV_ADD)], 0, 0)
                    except OSError as error:
                        assert error.errno == errno.EINVAL
                        print('ORIGINAL_HANDOFF_EINVAL_REPRODUCED', flush=True)
                    else:
                        raise AssertionError('The original macOS failure was not reproduced')
                    finally:
                        queue.close()
                    result = subprocess.run(runtime.interactive_command([
                        sys.executable, sys.argv[1], 'argument with spaces; not a shell command']),
                        stdin=terminal, env=dict(os.environ, CLAUDE_CONFIG_DIR=sys.argv[2]), check=False)
                    assert result.returncode == 7, result.returncode
                    print('CHILD_EXIT_STATUS_PRESERVED', flush=True)
            '''))
            env = dict(os.environ, PYTHONPATH=str(Path(runtime.__file__).parent))
            # The outer relay supplies a controlling terminal to the driver.
            result = subprocess.run(['/usr/bin/script', '-q', '/dev/null', sys.executable,
                                     str(driver), str(child), str(root / '.claude-acct-test')],
                                    cwd=root, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('ORIGINAL_HANDOFF_EINVAL_REPRODUCED', result.stdout)
            self.assertIn('NATIVE_CLI_TERMINAL_READY', result.stdout)
            self.assertIn('CHILD_EXIT_STATUS_PRESERVED', result.stdout)
            self.assertFalse((root / 'typescript').exists(), 'Never persist sign-in transcripts')


if __name__ == '__main__':
    unittest.main()
