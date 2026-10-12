import contextlib
import curses
import fcntl
import io
import json
import os
from pathlib import Path
import pty
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
from unittest.mock import patch

import account_status
import accounts_tui as tui


class Screen:
    def __init__(self):
        self.size = (30, 110)
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

    def test_first_run_enters_default_profile_wizard_and_reports_missing_cli(self):
        self.app.state = {'first_run': True, 'cli_available': True,
                          'accounts': [{'label': 'a', 'cli_state': 'missing'}]}
        self.assertEqual(self.app.items()[0][0], 'first_account')
        self.assertNotIn('account:a', [row[0] for row in self.app.items()])
        self.app.draw()
        self.assertTrue(any('WELCOME' in line for line in self.screen.text))
        self.app.key(10)
        self.assertEqual(self.app.label, 'a')
        self.assertEqual(self.app.wizard['stage'], 'cli')
        self.app.goto('home')
        self.app.state['cli_available'] = False
        self.app.dispatch('first_account')
        self.assertEqual(self.app.modal[2], 'Install Claude Code first')

    def test_installer_opens_new_user_setup_once_but_preserves_existing_account_menu(self):
        self.app.state = {'accounts': [{'label': 'a', 'cli_state': 'missing'}]}
        self.app.onboard = True
        self.app.status_ready()
        self.assertEqual(self.app.page, 'wizard')
        self.assertEqual(self.app.wizard['stage'], 'cli')
        self.assertFalse(self.app.onboard)
        self.app.goto('home')
        self.app.status_ready()
        self.assertEqual(self.app.page, 'home')
        self.app.onboard = True
        self.app.state['accounts'][0]['desktop_saved'] = True
        self.app.status_ready()
        self.assertEqual(self.app.page, 'home')

    def test_navigation_confirmation_and_cancel(self):
        self.app.key(curses.KEY_DOWN)
        self.app.key(curses.KEY_DOWN)
        self.app.key(10)
        self.assertEqual(self.app.account['label'], 'c')
        self.assertEqual(self.app.page, 'account')
        self.app.key(10)
        self.assertEqual(self.app.modal[0], 'confirm')
        self.app.key(27)
        self.assertIsNone(self.app.modal)
        self.assertIsNone(self.app.worker)
        self.app.key(27)
        self.assertEqual(self.app.page, 'home')

    def test_expired_login_guidance_and_cli_return(self):
        self.app.key(10)
        self.app.dispatch('save')
        self.assertIn('Verify this CLI login first', self.app.modal[3][0])
        self.app.key(27)
        self.app.dispatch('cli')
        self.assertEqual(self.app.modal[0], 'confirm')
        self.app.key(10)
        self.assertEqual(self.app.modal[2], 'Demo mode')

    def test_saved_desktop_can_switch_even_with_expired_cli(self):
        account = {'desktop_saved': True, 'cli_state': 'expired'}
        self.assertIsNone(tui.disabled_reason('switch', account, {}))
        self.assertIn('Verify', tui.disabled_reason('save', account, {}))
        self.assertIn('recovery', tui.disabled_reason('switch', account, {'recovery_pending': True}))
        self.app.dispatch('account:c')
        self.assertIn('switch', [item[0] for item in self.app.items()])
        self.assertNotIn('setup', [item[0] for item in self.app.items()])
        self.app.account['desktop_active'] = True
        for running in (True, False):
            self.app.state['desktop_running'] = running
            self.assertNotIn('switch', [item[0] for item in self.app.items()])
            self.assertIn('Already selected in Desktop.', self.app.header()[1])
            self.app.dispatch('switch')
            self.assertEqual(self.app.modal[0], 'message')
            self.assertIsNone(self.app.worker)
            self.app.key(27)

    def test_small_window_selected_account_stays_visible(self):
        self.screen.size = (20, 60)
        self.app.cursor = 2
        self.app.draw()
        self.assertTrue(any('hello@example.test' in line for line in self.screen.text))
        self.app.cursor = len(self.app.items())-1
        self.app.draw()
        self.assertTrue(any('Quit' in line for line in self.screen.text))
        self.screen.size = (12, 50)
        self.app.draw()
        self.assertTrue(any('Enlarge terminal' in line for line in self.screen.text))

    def test_add_account_input_cancel_and_reserved_label(self):
        self.app.dispatch('add')
        self.app.draw()
        self.assertEqual(self.app.wizard['stage'], 'name')
        self.app.key(ord('a'))
        self.app.key(10)
        self.assertIn('already exists', self.app.notice)
        self.app.key(127)
        for c in 'work':
            self.app.key(ord(c))
        self.assertEqual(self.app.input_text, 'work')
        with patch.object(self.app, 'start_onboarding') as start:
            self.app.key(10)
            start.assert_called_once_with('create')
        self.app.key(27)
        self.assertEqual(self.app.page, 'home')

    def test_every_wizard_step_renders_in_small_and_tall_terminals(self):
        self.app.dispatch('add')
        for size in [(20, 60), (100, 130)]:
            self.screen.size = size
            for stage in ('name', 'cli', 'desktop', 'finish', 'complete'):
                self.app.wizard['stage'] = stage
                self.app.draw()
                self.assertTrue(any('Enter:' in line for line in self.screen.text))

    def test_cli_auth_uses_selected_profile_and_returns_to_wizard(self):
        self.app.dispatch('account:b')
        self.app.dispatch('setup')
        self.app.wizard['stage'] = 'cli'
        self.app.demo = False
        with patch.object(tui.shutil, 'which', return_value='/bin/claude'), \
             patch.object(tui.subprocess, 'run') as run, \
             patch('curses.def_prog_mode'), patch('curses.endwin'), \
             patch('curses.reset_prog_mode') as restore, \
             patch.object(self.app, 'refresh') as refresh, \
             contextlib.redirect_stdout(io.StringIO()):
            run.return_value.returncode = 0
            self.app.launch_cli('b', authenticate=True)
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ['/bin/claude', 'auth', 'login', '--claudeai'])
            self.assertEqual(run.call_args.kwargs['env']['CLAUDE_CODE_SAFE_MODE'], '1')
            self.assertEqual(run.call_args.kwargs['env']['CLAUDE_CONFIG_DIR'], str(Path.home() / '.claude-acct-b'))
            refresh.assert_called_once_with('verify_cli')
            restore.assert_called_once()
            refresh.reset_mock()
            run.return_value.returncode = 1
            self.app.launch_cli('b', authenticate=True)
            refresh.assert_not_called()
            self.assertEqual(self.app.wizard['stage'], 'cli')
            self.assertIsNotNone(self.app.modal)
            self.assertEqual(self.app.modal[:2], ('confirm', 'wizard_cli'))
            self.app.key(10)
            self.assertEqual(run.call_args.args[0], ['/bin/claude', '--safe-mode'])
            self.assertEqual(run.call_args.kwargs['env']['CLAUDE_CONFIG_DIR'], str(Path.home() / '.claude-acct-b'))

    def test_new_profile_can_open_interactive_cli_before_status_has_loaded(self):
        self.app.label = 'claude-zeno'
        self.app.wizard = {'stage': 'cli'}
        self.app.goto('wizard')
        self.assertIsNone(self.app.account)
        self.assertEqual(self.app.items()[0][0], 'wizard_cli')
        with patch.object(self.app, 'launch_cli') as launch:
            self.app.key(10)
            launch.assert_called_once_with('claude-zeno')

    def test_enter_during_status_check_is_retained_and_opens_cli_once(self):
        self.app.label = 'claude-zeno'
        self.app.wizard = {'stage': 'cli'}
        self.app.goto('wizard')
        events = tui.queue.Queue()
        self.app.worker = SimpleNamespace(events=events)
        self.app.job = 'status'
        self.app.status_received = True
        with patch.object(self.app, 'launch_cli') as launch:
            self.app.key(10)
            launch.assert_not_called()
            self.assertIn('will open', self.app.notice)
            events.put(('done', 0))
            self.app.poll()
            launch.assert_called_once_with('claude-zeno')
            self.assertIsNone(self.app.deferred_action)

    def test_queued_action_is_not_applied_to_a_different_setup_stage(self):
        self.app.label = 'b'
        self.app.wizard = {'stage': 'cli'}
        self.app.goto('wizard')
        events = tui.queue.Queue()
        self.app.worker = SimpleNamespace(events=events)
        self.app.job = 'status'
        self.app.status_received = True
        self.app.after_status = 'verify_cli'
        self.app.key(10)
        events.put(('done', 0))
        with patch.object(self.app, 'launch_cli') as launch:
            self.app.poll()
            launch.assert_not_called()
        self.assertEqual(self.app.wizard['stage'], 'finish')
        self.assertIsNone(self.app.deferred_action)

    def test_failed_desktop_verification_returns_to_sign_in_instead_of_retry_loop(self):
        self.app.label = 'b'
        self.app.wizard = {'stage': 'finish'}
        self.app.goto('wizard')
        events = tui.queue.Queue()
        events.put(('line', 'Desktop must be signed into the intended account.'))
        events.put(('done', 1))
        self.app.worker = SimpleNamespace(events=events)
        self.app.job = 'onboard:finish'
        with patch.object(self.app, 'refresh'):
            self.app.poll()
        self.assertEqual(self.app.wizard['stage'], 'desktop')
        self.assertEqual(self.app.items()[0][0], 'prepare')

    def test_save_checks_live_desktop_and_requires_close_confirmation(self):
        self.app.label = 'b'
        self.app.wizard = {'stage': 'finish'}
        self.app.goto('wizard')
        self.app.demo = False
        self.app.state['desktop_running'] = False  # Stale header must not decide.
        with patch.object(tui.dp, 'desktop_state', return_value={'running': True, 'background': False}), \
             patch.object(self.app, 'start_onboarding') as start:
            self.app.dispatch('finish')
            start.assert_not_called()
            self.assertEqual(self.app.modal[:3], ('confirm', 'close_save:finish', 'Close Desktop and save?'))
            self.assertTrue(self.app.state['desktop_running'])
            self.app.key(27)
            start.assert_not_called()
            self.app.dispatch('finish')
            self.app.key(10)
            start.assert_called_once_with('finish')

    def test_closed_desktop_saves_without_close_prompt(self):
        self.app.label = 'b'
        self.app.wizard = {'stage': 'finish'}
        self.app.goto('wizard')
        self.app.demo = False
        with patch.object(tui.dp, 'desktop_state', return_value={'running': False, 'background': False}), \
             patch.object(self.app, 'start_onboarding') as start:
            self.app.dispatch('finish')
            start.assert_called_once_with('finish')
            self.assertIsNone(self.app.modal)

    def test_background_tasks_confirmation_runs_automatic_stop_and_save(self):
        self.app.label = 'b'
        self.app.wizard = {'stage': 'finish'}
        self.app.goto('wizard')
        self.app.demo = False
        with patch.object(tui.dp, 'desktop_state', return_value={'running': False, 'background': True}), \
             patch.object(self.app, 'start_onboarding') as start:
            self.app.dispatch('finish')
            self.assertEqual(self.app.modal[:3], ('confirm', 'stop_save:finish', 'Stop background tasks and save?'))
            self.assertIn('automatically', ' '.join(self.app.modal[3]))
            start.assert_not_called()
            self.app.key(27)
            start.assert_not_called()
            self.app.dispatch('finish')
            self.app.key(10)
            start.assert_called_once_with('finish')

    def test_busy_save_keeps_save_step_and_offers_retry_without_changing_login(self):
        self.app.label = 'b'
        self.app.wizard = {'stage': 'finish'}
        self.app.goto('wizard')
        events = tui.queue.Queue()
        events.put(('line', 'Setup stopped: DESKTOP_BUSY: Background tasks remain.'))
        events.put(('done', 1))
        self.app.worker = SimpleNamespace(events=events)
        self.app.job = 'onboard:finish'
        with patch.object(tui.dp, 'desktop_state', return_value={'running': False, 'background': True}), \
             patch.object(self.app, 'start_onboarding') as start:
            self.app.poll()
            self.assertEqual(self.app.wizard['stage'], 'finish')
            self.assertEqual(self.app.modal[:2], ('confirm', 'retry_save:finish'))
            self.assertTrue(self.app.state['desktop_background'])
            self.assertIn('Background tasks remain.', ' '.join(self.app.modal[3]))
            self.app.key(10)
            start.assert_called_once_with('finish')

    def test_new_profile_creation_refreshes_accounts_without_restart(self):
        self.app.label = 'claude-zeno'
        self.app.wizard = {'stage': 'name'}
        self.app.goto('wizard')
        events = tui.queue.Queue()
        events.put(('done', 0))
        self.app.worker = SimpleNamespace(events=events)
        self.app.job = 'onboard:create'
        with patch.object(self.app, 'refresh') as refresh:
            self.app.poll()
        self.assertEqual(self.app.wizard['stage'], 'cli')
        refresh.assert_called_once_with()

    def test_resume_pending_setup_and_demo_never_mutates(self):
        self.app.dispatch('account:b')
        self.assertIn('Finish Desktop sign-in', [item[1] for item in self.app.items()])
        self.app.account['pending'] = False
        self.assertIn('Set up Desktop login', [item[1] for item in self.app.items()])
        self.app.account['desktop_active'] = True
        self.assertNotIn('setup', [item[0] for item in self.app.items()])
        self.assertIn('save', [item[0] for item in self.app.items()])
        self.app.account['desktop_active'] = False
        self.app.account['pending'] = True
        self.app.dispatch('setup')
        self.assertEqual(self.app.wizard['stage'], 'finish')
        with patch.object(tui, 'Worker') as worker:
            self.app.dispatch('finish')
            worker.assert_not_called()
        self.assertEqual(self.app.modal[2], 'Demo mode')

    def test_cli_verification_advances_only_when_ready(self):
        self.app.dispatch('account:a')
        self.app.dispatch('setup')
        self.app.dispatch('verify_cli')
        self.assertEqual(self.app.wizard['stage'], 'cli')
        self.assertIsNotNone(self.app.modal)
        self.app.key(27)
        self.app.dispatch('back')
        self.app.dispatch('account:c')
        self.app.dispatch('setup')
        self.app.wizard['stage'] = 'cli'
        self.app.dispatch('verify_cli')
        self.assertEqual(self.app.wizard['stage'], 'desktop')

    def test_argument_and_environment_isolation(self):
        inherited = {'CLAUDE_CONFIG_DIR': '/wrong', 'CLAUDE_CODE_OAUTH_TOKEN': 'not-a-real-token', 'PATH': '/bin',
                     'ANTHROPIC_BASE_URL': 'https://wrong.example.test', 'CLAUDE_CODE_USE_BEDROCK': '1',
                     'CLAUDE_CODE_OAUTH_REFRESH_TOKEN': 'not-a-real-token'}
        env = tui.cli_environment('a', inherited)
        self.assertNotIn('CLAUDE_CONFIG_DIR', env)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertNotIn('CLAUDE_CODE_OAUTH_REFRESH_TOKEN', env)
        self.assertNotIn('ANTHROPIC_BASE_URL', env)
        self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', env)
        self.assertNotIn('CLAUDE_CODE_SAFE_MODE', env)
        self.assertEqual(tui.cli_environment('a', {'CLAUDE_CODE_SAFE_MODE': '1'})['CLAUDE_CODE_SAFE_MODE'], '1')
        self.assertEqual(inherited['CLAUDE_CONFIG_DIR'], '/wrong')
        self.assertEqual(tui.cli_environment('b', {}, Path('/tmp/test'))['CLAUDE_CONFIG_DIR'], '/tmp/test/.claude-acct-b')
        self.assertEqual(tui.helper_args('switch', 'b', True)[2:], ['--switch', 'b', '--dry-run'])
        self.assertEqual(tui.helper_args('sync', 'a')[2:], [])
        self.assertNotIn('\x1b', tui.clean('\x1b[31merror\x9b'))

    def test_cannot_quit_during_transaction(self):
        self.app.worker = object()
        self.app.key(ord('q'))
        self.assertTrue(self.app.running)

    def test_desktop_sign_in_refreshes_without_restarting_tui(self):
        self.app.dispatch('account:b')
        self.app.dispatch('setup')
        self.app.demo = False
        self.app.desktop_seen = ('old-login',)
        with patch.object(self.app, 'desktop_signature', return_value=('new-login',)), \
             patch.object(self.app, 'refresh') as refresh:
            self.app.poll()
            refresh.assert_called_once_with()
        self.app.account['desktop_active'] = True
        self.app.status_ready()
        self.assertEqual(self.app.wizard['stage'], 'finish')
        self.assertIn('sign-in detected', self.app.notice)
        self.app.account.update(desktop_saved=True, pending=False)
        self.app.status_ready()
        self.assertEqual(self.app.wizard['stage'], 'complete')

    def test_unchanged_desktop_does_not_keep_querying_accounts(self):
        self.app.dispatch('account:b')
        self.app.dispatch('setup')
        self.app.demo = False
        self.app.desktop_seen = ('same-login',)
        with patch.object(self.app, 'desktop_signature', return_value=('same-login',)), \
             patch.object(self.app, 'refresh') as refresh:
            self.app.poll()
            refresh.assert_not_called()

    def test_expiry_starts_background_refresh_without_blocking_navigation(self):
        self.app.demo = False
        row = self.app.state['accounts'][2]
        row.update(cli_state='ready', expires_at=time.time()-1, refresh_id='expired-generation')
        worker = SimpleNamespace(label='c', generation='expired-generation', events=tui.queue.Queue())
        with patch.object(tui, 'RefreshWorker', return_value=worker) as start:
            self.app.poll()
            start.assert_called_once_with('c', 'expired-generation', force=False)
            self.assertEqual(row['cli_state'], 'refreshing')
            self.app.dispatch('account:c')
            self.assertEqual(self.app.page, 'account')
            self.app.key(curses.KEY_DOWN)
            self.assertEqual(self.app.cursor, 1)
            with patch.object(self.app, 'launch_cli') as login:
                self.app.dispatch('auth')
                login.assert_not_called()
            worker.events.put({'label': 'c', 'before_id': 'expired-generation', 'refresh_id': 'expired-generation',
                               'cli_state': 'sign_in_required', 'cli_detail': 'Renewal failed; sign in again.'})
            self.app.poll()
            self.assertEqual(row['cli_state'], 'sign_in_required')
            self.assertIsNone(self.app.modal, 'Do not launch an unsolicited sign-in dialog')
            row['cli_state'] = 'expired'  # A subsequent read-only status response.
            self.app.apply_refresh_results()
            self.app.poll()
            self.assertEqual(row['cli_state'], 'sign_in_required')
            self.assertEqual(start.call_count, 1)

    def test_background_success_rechecks_status_and_updates_badge(self):
        self.app.demo = False
        row = self.app.state['accounts'][2]
        row.update(cli_state='refreshing', refresh_id='old')
        worker = SimpleNamespace(label='c', generation='old', events=tui.queue.Queue())
        self.app.cli_worker = worker
        worker.events.put({'label': 'c', 'before_id': 'old', 'refresh_id': 'new', 'cli_state': 'ready',
                           'cli_detail': 'Refreshed.', 'email': 'hello@example.test', 'account': 'c-id',
                           'expires_at': time.time()+3600})
        with patch.object(self.app, 'refresh') as refresh:
            self.app.poll()
            refresh.assert_called_once_with()
        self.assertEqual(row['cli_state'], 'ready')
        self.assertEqual(row['refresh_id'], 'new')
        self.assertIsNone(self.app.modal)

    def test_automatic_refresh_advances_onboarding_without_manual_verification(self):
        for components, active, saved, pending, expected in [
                (['cli', 'desktop'], False, False, False, 'desktop'),
                (['cli', 'desktop'], True, False, False, 'finish'),
                (['cli', 'desktop'], True, True, False, 'complete'),
                (['cli', 'desktop'], False, False, True, 'finish'),
                (['cli'], False, False, False, 'complete')]:
            with self.subTest(components=components, active=active, saved=saved, pending=pending):
                self.app = tui.App(self.screen, demo=True)
                self.app.components = components
                self.app.label = 'a'
                row = self.app.account
                row.update(cli_state='expired', refresh_id='old', desktop_active=active,
                           desktop_saved=saved, pending=pending)
                self.app.state['active_account'] = 'a-id' if active else 'other-id'
                self.app.begin_setup()
                self.assertEqual(self.app.wizard['stage'], 'cli')
                self.assertIn('existing CLI login', self.app.header()[0])
                self.assertFalse({'wizard_cli', 'wizard_auth'} & {r[0] for r in self.app.items()})
                self.assertIsNone(self.app.pending_cli_check)
                worker = SimpleNamespace(label='a', generation='old', events=tui.queue.Queue())
                self.app.cli_worker = worker
                worker.events.put({'label': 'a', 'before_id': 'old', 'refresh_id': 'new',
                                   'cli_state': 'ready', 'cli_detail': 'Refreshed.',
                                   'email': 'existing@example.test', 'account': 'a-id',
                                   'expires_at': time.time()+3600})
                with patch.object(self.app, 'launch_cli') as launch:
                    self.app.poll_cli_refresh()
                    launch.assert_not_called()
                self.assertEqual(self.app.wizard['stage'], expected)
                self.assertIsNone(self.app.modal)

    def test_status_detection_advances_cli_step_only_for_a_verified_selected_profile(self):
        self.app.label = 'a'
        self.app.begin_setup()
        for state in ('missing', 'expired', 'refreshing', 'unverified', 'sign_in_required'):
            self.app.account['cli_state'] = state
            self.app.status_ready()
            self.assertEqual(self.app.wizard['stage'], 'cli')
        self.app.account.update(cli_state='ready', desktop_active=False)
        self.app.deferred_action = ('wizard', 'a', 'cli', 'wizard_cli')
        self.app.status_ready()
        self.assertEqual(self.app.wizard['stage'], 'desktop')
        self.assertIsNone(self.app.deferred_action)

    def test_failed_automatic_refresh_keeps_cli_step_and_offers_sign_in(self):
        self.app.label = 'a'
        self.app.account.update(cli_state='expired', refresh_id='old')
        self.app.begin_setup()
        worker = SimpleNamespace(label='a', generation='old', events=tui.queue.Queue())
        self.app.cli_worker = worker
        worker.events.put({'label': 'a', 'before_id': 'old', 'refresh_id': 'old',
                           'cli_state': 'sign_in_required', 'cli_detail': 'Sign in again.'})
        self.app.poll_cli_refresh()
        self.assertEqual(self.app.wizard['stage'], 'cli')
        self.assertEqual(self.app.items()[0][0], 'wizard_cli')


class StatusTests(unittest.TestCase):
    def test_expired_token_never_sent_and_no_secrets_in_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.claude-acct-b').mkdir()
            def creds(config):
                return {'accessToken': 'secret-that-must-not-appear', 'expiresAt': 1 if config is None else (time.time() + 300) * 1000}
            def identity(config):
                self.assertIsNotNone(config, 'expired token must not be sent')
                return 'b-id', 'org-id', 'verified@example.test'
            with patch.object(account_status.dp, 'desktop_account', return_value=None), patch.object(account_status.dp, 'desktop_state', return_value={'running': False, 'background': False}):
                result = account_status.collect(home, creds, identity)
            self.assertEqual(result['accounts'][0]['cli_state'], 'expired')
            self.assertIsNone(result['accounts'][0]['email'])
            self.assertEqual(result['accounts'][1]['email'], 'verified@example.test')
            self.assertNotIn('secret-that-must-not-appear', json.dumps(result))


class TerminalTests(unittest.TestCase):
    def test_real_terminal_navigation_resize_and_exit(self):
        master, slave = pty.openpty()
        initial = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 110, 0, 0))
        process = subprocess.Popen([sys.executable, str(Path(tui.__file__)), '--demo'],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True, env={**os.environ, 'TERM': 'xterm-256color'})
        output = bytearray()
        def expect(text):
            deadline = time.monotonic() + 5
            while text.encode() not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output.extend(os.read(master, 65536))
            self.assertIn(text.encode(), output)
        try:
            expect('CLAUDE ACCOUNTS')
            os.write(master, b'jj\r\r')
            expect('Switch Desktop account?')
            os.write(master, b'\x1b')
            # Wait for Escape's disambiguation before the next keystroke.
            time.sleep(1.1)
            os.write(master, b'?')
            expect('Using Claude Accounts')
            os.write(master, b'\r')
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 12, 50, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            expect('Enlarge terminal')
            os.write(master, b'q')
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output.extend(os.read(master, 65536))
            process.wait(timeout=1)
            self.assertEqual(process.returncode, 0)
            restored = termios.tcgetattr(slave)
            # macOS may set PENDIN when the unread input queue changes on resize.
            restored[3] &= ~getattr(termios, 'PENDIN', 0)
            initial[3] &= ~getattr(termios, 'PENDIN', 0)
            self.assertEqual(restored, initial)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)
            os.close(slave)


if __name__ == '__main__':
    unittest.main()
