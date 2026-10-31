import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import patch

import accounts_tui
import codex_tui
import provider_app
import setup_options
from test_codex_tui import Screen


class ProviderTests(unittest.TestCase):
    def test_explicit_codex_cli_and_help_work_without_a_terminal(self):
        for args in (['--cli', 'work', '--', 'exec', 'hello'], ['--help']):
            with self.subTest(args=args), patch.object(sys.stdin, 'isatty', return_value=False), \
                 patch.object(sys.stdout, 'isatty', return_value=False), \
                 patch.object(codex_tui, 'main', return_value=7) as run, \
                 patch.object(provider_app, 'choose') as choose:
                self.assertEqual(provider_app.main(['--provider', 'codex', *args]), 7)
                run.assert_called_once_with(args)
                choose.assert_not_called()

    def test_legacy_cli_still_routes_to_claude(self):
        with patch.object(sys.stdin, 'isatty', return_value=False), \
             patch.object(accounts_tui, 'main', return_value=0) as run:
            self.assertEqual(provider_app.main(['--cli', 'work', '--', '-p', 'hello']), 0)
            run.assert_called_once_with(['--cli', 'work', '--', '-p', 'hello'])

    def test_noninteractive_codex_exec_is_forwarded_to_profile_helper(self):
        with patch.object(sys.stdin, 'isatty', return_value=False), \
             patch.object(sys.stdout, 'isatty', return_value=False), \
             patch.object(codex_tui.subprocess, 'call', return_value=0) as run:
            self.assertEqual(provider_app.main(['--provider', 'codex', '--cli', 'work', '--', 'exec', 'hello']), 0)
            self.assertEqual(run.call_args.args[0][-5:], ['--launch-cli', 'work', '--', 'exec', 'hello'])

    def test_back_returns_to_provider_selection_without_restarting_onboarding(self):
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stdout, 'isatty', return_value=True), \
             patch.object(codex_tui, 'main', return_value=20) as codex, \
             patch.object(accounts_tui, 'main', return_value=0) as claude, \
             patch.object(provider_app, 'choose', return_value='claude'):
            self.assertEqual(provider_app.main(['--provider', 'codex', '--onboard', '--demo']), 0)
            codex.assert_called_once_with(['--onboard', '--demo'])
            claude.assert_called_once_with(['--demo'])

    def test_cancel_and_interrupt_do_not_open_a_provider(self):
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stdout, 'isatty', return_value=True), \
             patch.object(codex_tui, 'main') as codex, patch.object(accounts_tui, 'main') as claude, \
             patch.object(provider_app, 'choose', return_value=None) as choose:
            self.assertEqual(provider_app.main([]), 0)
            choose.side_effect = KeyboardInterrupt
            self.assertEqual(provider_app.main([]), 130)
            codex.assert_not_called()
            claude.assert_not_called()

    def test_component_cancel_restores_previous_interrupt_handler(self):
        previous = signal.getsignal(signal.SIGINT)
        screen = Screen()
        def getch():
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            return -1
        screen.getch = getch
        with patch('curses.wrapper', side_effect=lambda callback:callback(screen)), \
             patch('curses.has_colors', return_value=False), patch('curses.curs_set'):
            self.assertIsNone(setup_options.choose({}, ['cli'], provider='codex'))
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_codex_and_combined_component_selector_use_correct_brand(self):
        with patch('curses.has_colors', return_value=False), patch('curses.curs_set'):
            for provider, name in (('codex', 'CODEX ACCOUNTS'), ('both', 'CLAUDE + CODEX ACCOUNTS')):
                screen = Screen()
                selector = setup_options.Selector(screen, {'cli': True}, ['cli'], provider)
                selector.draw()
                self.assertIn(name, ' '.join(screen.text))
                self.assertIn('✓ Detected', screen.text)


class ProviderTerminalTests(unittest.TestCase):
    def test_back_from_codex_then_ctrl_c_cleans_only_tui(self):
        master, slave = pty.openpty()
        initial = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 110, 0, 0))
        process = subprocess.Popen([sys.executable, str(Path(provider_app.__file__)), '--provider', 'codex', '--demo'],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                   env={**os.environ, 'TERM': 'xterm-256color'})
        output = bytearray()
        def expect(value):
            deadline = time.monotonic()+5
            while value not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    output.extend(os.read(master, 65536))
            self.assertIn(value, output)
        try:
            expect(b'CODEX ACCOUNTS')
            os.write(master, b'\x1b')
            expect(b'Choose the provider to manage')
            process.send_signal(signal.SIGINT)
            deadline = time.monotonic()+5
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    output.extend(os.read(master, 65536))
            process.wait(timeout=1)
            while select.select([master], [], [], .02)[0]:
                output.extend(os.read(master, 65536))
            self.assertEqual(process.returncode, 130)
            self.assertNotIn(b'Traceback', output)
            self.assertGreaterEqual(output.count(b'\x1b[?1049l'), 2)
            self.assertNotIn(b'\x1b[3J', output)
            self.assertNotIn(b'\x1b[2J', output[output.rfind(b'\x1b[?1049l'):])
            restored = termios.tcgetattr(slave)
            for attrs in (initial, restored):
                attrs[3] &= ~getattr(termios, 'PENDIN', 0)
            self.assertEqual(initial, restored)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            os.close(master); os.close(slave)


if __name__ == '__main__':
    unittest.main()
