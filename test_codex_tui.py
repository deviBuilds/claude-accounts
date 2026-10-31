import curses
import fcntl
import os
from pathlib import Path
import pty
import queue
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import codex_tui as tui


class Screen:
    def __init__(self):
        self.size = (40, 110)
        self.text = []
    def getmaxyx(self): return self.size
    def keypad(self, _): pass
    def timeout(self, _): pass
    def erase(self): self.text = []
    def refresh(self): pass
    def clear(self): pass
    def addnstr(self, y, x, text, width, attr): self.text.append(text[:width])


class TuiTests(unittest.TestCase):
    def setUp(self):
        patch('curses.has_colors', return_value=False).start()
        patch('curses.curs_set').start()
        self.addCleanup(patch.stopall)
        self.screen = Screen()
        self.app = tui.App(self.screen, demo=True)

    def test_account_navigation_and_unified_login_management(self):
        self.app.key(10)
        self.assertEqual(self.app.label, 'personal')
        self.assertEqual(self.app.page, 'account')
        self.assertNotIn('switch', [r[0] for r in self.app.items()])
        self.app.dispatch('manage')
        self.assertEqual(self.app.items()[0][0], 'change-login')
        self.assertIn('CLI + Desktop', self.app.items()[0][1])
        self.app.key(27)
        self.assertEqual(self.app.page, 'account')
        self.app.key(27)
        self.assertEqual(self.app.page, 'home')

    def test_add_wizard_space_selection_validation_and_demo_safety(self):
        self.app.dispatch('add')
        self.assertEqual(self.app.wizard['stage'], 'components')
        self.app.key(32)
        self.assertEqual(self.app.components, ['desktop'])
        self.app.dispatch('components_done')
        self.assertEqual(self.app.wizard['stage'], 'name')
        self.app.input_text = 'Not valid'
        self.app.key(10)
        self.assertIn('lowercase', self.app.notice)
        self.app.input_text = 'new-profile'
        self.app.key(10)
        self.assertEqual(self.app.modal[2], 'Demo mode')
        self.assertIsNone(self.app.worker)

    def test_existing_verified_login_advances_setup_without_signin(self):
        self.app.label = 'personal'
        self.app.begin_setup(existing=True)
        self.assertEqual(self.app.wizard['stage'], 'ready')
        self.assertNotIn('login', [r[0] for r in self.app.items()])
        self.assertNotIn('switch', [r[0] for r in self.app.items()])

    def test_desktop_switch_confirmation_is_explicit_and_cancellable(self):
        self.app.dispatch('account:work')
        self.app.dispatch('switch')
        self.assertEqual(self.app.modal[0], 'confirm')
        self.assertTrue(any('will quit and reopen' in line for line in self.app.modal[3]))
        self.app.key(27)
        self.assertIsNone(self.app.modal)
        self.assertIsNone(self.app.worker)

    def test_selected_account_can_open_a_closed_managed_desktop(self):
        self.app.state['desktop_managed_running'] = False
        self.app.dispatch('account:personal')
        self.assertEqual(self.app.items()[0][:2], ('switch', 'Open Codex Desktop'))
        self.app.dispatch('switch')
        self.assertEqual(self.app.modal[0], 'confirm')
        self.assertTrue(any('unmanaged Desktop stays open' in line for line in self.app.modal[3]))

    def test_rotation_under_tools_valid_profiles_only_and_space_toggles(self):
        self.assertNotIn('rotation', [r[0] for r in self.app.items()])
        self.app.dispatch('tools')
        self.assertIn('rotation', [r[0] for r in self.app.items()])
        self.app.dispatch('rotation')
        self.assertEqual([r[0] for r in self.app.items() if r[0].startswith('rotate_pick:')],
                         ['rotate_pick:personal', 'rotate_pick:work'])
        self.app.key(32)
        self.assertEqual(self.app.rotation_selected, ['personal'])
        self.app.dispatch('rotate_enable')
        self.assertEqual(self.app.modal[2], 'Select accounts')
        self.app.key(27)
        self.app.dispatch('rotate_pick:work')
        self.app.dispatch('rotate_enable')
        self.assertTrue(any('Running clients keep their login' in line for line in self.app.modal[3]))
        self.app.key(10)
        self.assertEqual(self.app.modal[2], 'Demo mode')

    def test_rotation_prunes_expired_selection_and_requires_two(self):
        self.app.rotation_selected = ['personal', 'work']
        self.app.state['accounts'][1]['cli_state'] = 'sign_in_required'
        self.app.dispatch('rotation')
        self.app.state['accounts'][1]['cli_state'] = 'sign_in_required'
        self.app.items()
        self.assertEqual(self.app.rotation_selected, ['personal'])

    def test_api_key_is_usable_but_not_rotation_eligible(self):
        self.app.state['accounts'][0]['cli_state'] = 'api_key'
        self.app.label = 'personal'
        self.app.begin_setup(existing=True)
        self.assertEqual(self.app.wizard['stage'], 'ready')
        self.assertNotIn('personal', [r['label'] for r in self.app.rotation_accounts()])

    def test_external_login_requires_managed_login_without_claiming_expiry(self):
        row = self.app.state['accounts'][0]
        row.update(cli_state='external', cli_detail='Sign in to a managed profile; the app login is retained.')
        self.app.draw()
        self.assertIn('MANAGED LOGIN NEEDED', ' '.join(self.screen.text))
        self.assertNotIn('personal', [r['label'] for r in self.app.rotation_accounts()])
        self.app.dispatch('account:personal')
        self.assertEqual(self.app.items()[0][:2], ('change-login', 'Set up managed login'))
        self.assertEqual(self.app.rotation_health({'state': 'waiting_idle', 'enabled': True}), ('WAITING FOR IDLE', 15))

    def test_nonstandard_usage_windows_are_rendered_without_five_hour_assumption(self):
        row = self.app.state['accounts'][0]
        row['usage']['windows'] = [{'key': 'primary', 'window_minutes': 120, 'percent': 20, 'resets_at': time.time()+400},
                                   {'key': 'secondary', 'window_minutes': 1440, 'percent': 30, 'resets_at': time.time()+800}]
        self.app.draw()
        text = ' '.join(self.screen.text)
        self.assertIn('2h [', text)
        self.assertIn('1d [', text)
        self.app.dispatch('account:personal')
        self.app.dispatch('usage')
        self.assertTrue(any(line.startswith('2h:') for line in self.app.modal[3]))

    def test_status_refresh_keeps_enter_action_until_ready(self):
        self.app.demo = False
        self.app.worker = SimpleNamespace(events=queue.Queue())
        self.app.job = 'status'
        self.app.status_received = False
        self.app.key(10)
        self.assertEqual(self.app.page, 'home')
        self.app.worker.events.put(('status', self.app.state))
        self.app.worker.events.put(('done', 0))
        self.app.poll()
        self.assertEqual(self.app.page, 'account')
        self.assertEqual(self.app.label, 'personal')

    def test_helper_routing_never_uses_claude_commands(self):
        self.app.demo = False
        self.app.label = 'work'
        with patch.object(tui, 'Worker') as worker:
            self.app.start('verify')
            command = worker.call_args.args[0]
            self.assertEqual(command[-2:], ['--verify', 'work'])
            self.assertIn('codex_accounts.py', command[-3])
            self.app.worker = None
            self.app.start('restore:kept')
            self.assertEqual(worker.call_args.args[0][-2:], ['--restore', 'kept'])
            self.app.worker = None
            self.app.rotation_selected = ['personal', 'work']
            self.app.start('rotate_enable')
            command = worker.call_args.args[0]
            self.assertEqual(command[-3:], ['--enable', 'personal', 'work'])
            self.assertIn('codex_rotation.py', command[-4])

    def test_change_login_opens_signin_after_archiving(self):
        self.app.demo = False
        self.app.label = 'work'
        self.app.worker = SimpleNamespace(events=queue.Queue())
        self.app.job = 'change-login'
        self.app.worker.events.put(('done', 0))
        with patch.object(self.app, 'launch_cli') as launch:
            self.app.poll()
        launch.assert_called_once_with(authenticate=True)

    def test_every_menu_option_remains_reachable_in_short_terminal(self):
        self.screen.size = (20, 60)
        for page in ('home', 'account', 'manage', 'tools', 'removed', 'rotation'):
            self.app.page, self.app.label = page, 'work'
            for index, (action, title, _) in enumerate(self.app.items()):
                self.app.cursor = index
                self.app.draw()
                rendered = ' '.join(self.screen.text)
                self.assertIn('CODEX ACCOUNTS', rendered)
                if action.startswith(('account:', 'rotate_pick:')):
                    self.assertIn(action.split(':', 1)[1]+']', rendered)
                else:
                    self.assertIn(title, rendered)

    def test_confirmation_panel_uses_shared_modal_layout(self):
        self.app.dispatch('account:work')
        self.app.dispatch('remove')
        self.app.draw()
        self.assertIn('CONFIRM ACTION', ' '.join(self.screen.text))
        self.assertFalse(self.app._modal_paint)

    def test_provider_back_has_distinct_exit_code(self):
        self.app.dispatch('providers')
        with patch.object(tui.BaseApp, 'run', return_value=0):
            self.assertEqual(self.app.run(), 20)

    def test_component_preferences_are_codex_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            claude = home/'.config/claude-accounts/preferences.json'
            claude.parent.mkdir(parents=True)
            claude.write_text('{"components":["cli"]}')
            tui.save_components(['desktop'], home)
            self.assertEqual(tui.load_components(home), ['desktop'])
            self.assertEqual(claude.read_text(), '{"components":["cli"]}')


class TerminalTests(unittest.TestCase):
    def test_real_terminal_navigation_and_ctrl_c_restore_screen(self):
        master, slave = pty.openpty()
        initial = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 110, 0, 0))
        process = subprocess.Popen([sys.executable, str(Path(tui.__file__)), '--demo'],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                   env={**os.environ, 'TERM': 'xterm-256color'})
        output = bytearray()
        def expect(value):
            deadline = time.monotonic()+5
            while value not in output and time.monotonic()<deadline:
                if select.select([master], [], [], .05)[0]:
                    output.extend(os.read(master, 65536))
            self.assertIn(value, output)
        try:
            expect(b'CODEX ACCOUNTS')
            os.write(master, b'j\r')
            expect(b'Accounts / work@example.test')
            os.write(master, b'\r')
            expect(b'CONFIRM ACTION')
            process.send_signal(signal.SIGINT)
            deadline = time.monotonic()+5
            while process.poll() is None and time.monotonic()<deadline:
                if select.select([master], [], [], .05)[0]:
                    output.extend(os.read(master, 65536))
            process.wait(timeout=1)
            while select.select([master], [], [], .02)[0]:
                output.extend(os.read(master, 65536))
            self.assertEqual(process.returncode, 130)
            self.assertNotIn(b'Traceback', output)
            self.assertIn(b'\x1b[?1049l', output)
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
