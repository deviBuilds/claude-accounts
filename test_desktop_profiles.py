"""Transaction tests use disposable fixture profiles and never control the real app."""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import desktop_profiles as dp


class DesktopLifecycleTests(unittest.TestCase):
    def test_process_scan_excludes_crash_reporting_and_zombies_but_keeps_sessions(self):
        runtime = str(Path.home() / 'Library/Application Support/Claude/claude-code')
        output = '\n'.join([
            f'10 1 S {dp.APP}/Contents/MacOS/Claude',
            f'11 10 S {dp.APP}/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper',
            f'12 10 S {dp.APP}/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler',
            f'13 10 Z {dp.APP}/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper',
            f'20 1 S {runtime}/version/claude',
            '21 20 S /usr/bin/node', '30 1 S /usr/local/bin/claude'])
        with patch.object(dp.subprocess, 'run', return_value=SimpleNamespace(stdout=output, returncode=0)):
            self.assertEqual(dp.desktop_processes(), {10, 11, 20, 21})
            self.assertEqual(dp.desktop_state(), {'running': True, 'background': False})
        with patch.object(dp, 'process_records', return_value=[(20, 1, runtime + '/version/claude')]):
            self.assertEqual(dp.desktop_state(), {'running': False, 'background': True})

    def test_crash_reporter_alone_does_not_block_a_closed_app_save(self):
        records = [(12, 1, dp.APP + '/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler')]
        with patch.object(dp, 'process_records', return_value=records), patch.object(dp.subprocess, 'run') as run:
            self.assertEqual(dp.desktop_state(), {'running': False, 'background': False})
            dp.quit_app()
            run.assert_not_called()

    def test_orphaned_child_is_stopped_and_standalone_cli_is_untouched(self):
        initial = [(10, 1, dp.APP + '/Contents/MacOS/Claude'), (21, 10, '/usr/bin/node')]
        orphan = [(21, 1, '/usr/bin/node'), (30, 1, '/usr/local/bin/claude')]
        with patch.object(dp, 'process_records', side_effect=[initial] + [orphan]*31 + [[]]), \
             patch.object(dp, 'process_identity', side_effect=lambda pid: str(pid)), \
             patch.object(dp.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(dp.time, 'sleep'), patch.object(dp.os, 'kill') as kill:
            dp.quit_app()
            self.assertEqual(run.call_args.args[0][0], 'osascript')
            kill.assert_called_once_with(21, signal.SIGTERM)

    def test_stubborn_tasks_are_force_stopped_before_returning(self):
        records = [(20, 1, dp.APP + '/Contents/Helper')]
        alive = True
        def kill(pid, sig):
            nonlocal alive
            if sig == signal.SIGKILL:
                alive = False
        with patch.object(dp, 'process_records', side_effect=lambda: records if alive else []), \
             patch.object(dp, 'process_identity', return_value='original'), \
             patch.object(dp.time, 'sleep'), patch.object(dp.os, 'kill', side_effect=kill) as send:
            dp.quit_app()
            self.assertEqual(send.call_args_list, [unittest.mock.call(20, signal.SIGTERM),
                                                  unittest.mock.call(20, signal.SIGKILL)])

    def test_reused_orphan_pid_and_its_children_are_not_signalled(self):
        initial = [(10, 1, dp.APP + '/Contents/MacOS/Claude'), (21, 10, '/usr/bin/node')]
        reused = [(21, 1, '/usr/bin/node'), (22, 21, '/usr/bin/node')]
        with patch.object(dp, 'process_records', side_effect=[initial, reused]), \
             patch.object(dp, 'process_identity', side_effect=['main birth', 'old birth', 'new birth']), \
             patch.object(dp.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(dp.os, 'kill') as kill:
            dp.quit_app()
            kill.assert_not_called()

    def test_shutdown_permission_failure_prevents_save(self):
        records = [(20, 1, dp.APP + '/Contents/Helper')]
        with patch.object(dp, 'process_records', return_value=records), \
             patch.object(dp, 'process_identity', return_value='original'), \
             patch.object(dp.os, 'kill', side_effect=PermissionError):
            with self.assertRaisesRegex(dp.DesktopBusyError, 'macOS denied'):
                dp.quit_app()

    def test_manager_host_is_protected(self):
        records = [(20, 1, dp.APP + '/Contents/Helper'), (os.getpid(), 20, '/usr/bin/python')]
        with patch.object(dp, 'process_records', return_value=records), patch.object(dp.os, 'kill') as kill:
            with self.assertRaisesRegex(dp.DesktopBusyError, 'separate Terminal'):
                dp.quit_app()
            kill.assert_not_called()

    def test_main_app_quits_gracefully_and_ignores_surviving_crash_reporter(self):
        initial = [(10, 1, dp.APP + '/Contents/MacOS/Claude')]
        leftover = [(12, 1, dp.APP + '/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler')]
        with patch.object(dp, 'process_records', side_effect=[initial, leftover]), \
             patch.object(dp, 'process_identity', return_value='original'), \
             patch.object(dp.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            dp.quit_app()
            self.assertEqual(run.call_args.args[0][0], 'osascript')

    def test_apple_event_timeout_still_stops_owned_tasks(self):
        records = [(10, 1, dp.APP + '/Contents/MacOS/Claude')]
        alive = True
        def kill(*args):
            nonlocal alive
            alive = False
        with patch.object(dp, 'process_records', side_effect=lambda: records if alive else []), \
             patch.object(dp, 'process_identity', return_value='original'), \
             patch.object(dp.subprocess, 'run', side_effect=subprocess.TimeoutExpired('osascript', 15)), \
             patch.object(dp.time, 'sleep'), patch.object(dp.os, 'kill', side_effect=kill) as send:
            dp.quit_app()
            send.assert_called_once_with(10, signal.SIGTERM)

    def test_real_disposable_process_shutdown_keeps_unrelated_process_alive(self):
        # Only these child PIDs are exposed to the shutdown routine. No real app control.
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready'
            target = subprocess.Popen([sys.executable, '-c',
                'import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                'pathlib.Path(__import__("sys").argv[1]).touch(); time.sleep(60)', str(ready)])
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            try:
                import time
                deadline = time.monotonic() + 5
                while not ready.exists() and target.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
                def records():
                    values = [(unrelated.pid, os.getpid(), '/usr/local/bin/claude')]
                    if target.poll() is None:
                        values.append((target.pid, os.getpid(), dp.APP + '/Contents/TestHelper'))
                    return values
                with patch.object(dp, 'process_records', side_effect=records):
                    dp.quit_app()
                self.assertEqual(target.wait(timeout=2), -signal.SIGKILL)
                self.assertIsNone(unrelated.poll())
            finally:
                for child in (target, unrelated):
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ids = {
            'b': ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '11111111-1111-1111-1111-111111111111', 'b@example.test'),
            'c': ('cccccccc-cccc-cccc-cccc-cccccccccccc', '22222222-2222-2222-2222-222222222222', 'c@example.test'),
        }
        for label in self.ids:
            (self.home / ('.claude-acct-' + label)).mkdir()
        def identity(config):
            if config is None:
                raise RuntimeError('Default CLI login unavailable in this fixture')
            return self.ids[config.name[-1]]
        self.manager = dp.Profiles(self.home, identity, None)
        self.root = self.manager.root
        self.root.mkdir(parents=True)
        self.set_account('c')
        (self.root / 'Cache').mkdir()
        (self.root / 'Cache/cache').write_text('discardable')
        (self.root / 'Local Storage').mkdir()
        (self.root / 'Local Storage/state').write_text('hello state')
        self.addCleanup(patch.stopall)
        patch.object(dp, 'desktop_account', side_effect=self.account).start()
        self.quit = patch.object(dp, 'quit_app').start()
        self.open = patch.object(dp, 'open_app').start()
        patch.object(dp, 'app_running', return_value=True).start()
        patch('sys.stdout', new_callable=io.StringIO).start()
        with self.manager.locked():
            pass

    def set_account(self, label):
        (self.root / 'config.json').write_text(json.dumps({'account': self.ids[label][0]}))
        (self.root / 'Cookies').write_text('encrypted fixture ' + label)

    def account(self, root):
        p = root / 'config.json'
        return json.loads(p.read_text())['account'] if p.exists() else None

    def entry(self, label, name, content, mtime):
        account, org, _ = self.ids[label]
        p = self.root / 'claude-code-sessions' / account / org / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        os.utime(p, (mtime, mtime))
        return p

    def save_both(self):
        self.manager.save('c')
        self.manager.switch('b', login=True)
        self.set_account('b')
        self.manager.save('b')

    def test_wrong_label_and_dry_run_leave_profile_untouched(self):
        with self.assertRaisesRegex(RuntimeError, 'not signed in'):
            self.manager.save('b')
        self.manager.save('c', dry=True)
        self.quit.assert_not_called()
        self.assertFalse(self.manager.index_path.exists())
        self.assertEqual(self.account(self.root), self.ids['c'][0])

    def test_save_preserves_auth_and_state_and_omits_cache(self):
        self.manager.save('c')
        saved = self.manager.index()['profiles']['c']
        data = self.manager.snapshot_path(saved['snapshot']) / 'data'
        self.assertEqual((data / 'Cookies').read_text(), 'encrypted fixture c')
        self.assertEqual((data / 'Local Storage/state').read_text(), 'hello state')
        self.assertFalse((data / 'Cache').exists())
        self.assertEqual(self.manager.store.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.manager.index_path.stat().st_mode & 0o777, 0o600)

    def test_clean_login_and_round_trip_retain_each_login(self):
        self.save_both()
        (self.root / 'b-only-state').write_text('new work')
        self.manager.switch('c')
        self.assertEqual((self.root / 'Cookies').read_text(), 'encrypted fixture c')
        self.assertFalse((self.root / 'b-only-state').exists())
        self.assertEqual((self.root / 'Local Storage/state').read_text(), 'hello state')
        self.manager.switch('b')
        self.assertEqual((self.root / 'Cookies').read_text(), 'encrypted fixture b')
        self.assertEqual((self.root / 'b-only-state').read_text(), 'new work')
        self.assertTrue(list(self.manager.store.glob('rollback-*')))

    def test_newest_code_history_shared_but_schedules_stay_per_profile(self):
        self.entry('c', 'local_shared.json', 'old', 100)
        self.entry('c', 'scheduled-tasks.json', 'hello schedule', 100)
        self.save_both()
        self.entry('b', 'local_shared.json', 'new', 200)
        self.entry('b', 'local_extra.json', 'extra', 200)
        self.entry('b', 'scheduled-tasks.json', 'ankit schedule', 200)
        self.manager.switch('c')
        for label in ('b', 'c'):
            account, org, _ = self.ids[label]
            folder = self.root / 'claude-code-sessions' / account / org
            self.assertEqual((folder / 'local_shared.json').read_text(), 'new')
            self.assertEqual((folder / 'local_extra.json').read_text(), 'extra')
        account, org, _ = self.ids['c']
        self.assertEqual((self.root / 'claude-code-sessions' / account / org / 'scheduled-tasks.json').read_text(), 'hello schedule')

    def test_failed_staging_leaves_active_profile(self):
        self.save_both()
        with patch.object(dp.shutil, 'copytree', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.manager.switch('c')
        self.assertEqual(self.account(self.root), self.ids['b'][0])
        self.assertFalse(self.manager.journal.exists())

    def test_failed_commit_rolls_back_active_profile_and_index(self):
        self.save_both()
        previous = self.manager.index()
        real_write = dp.atomic_json
        failed = False
        def fail_once(path, value):
            nonlocal failed
            if path == self.manager.index_path and not failed:
                failed = True
                raise OSError('simulated interrupted commit')
            real_write(path, value)
        with patch.object(dp, 'atomic_json', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'interrupted commit'):
                self.manager.switch('c')
        self.assertEqual(self.account(self.root), self.ids['b'][0])
        self.assertEqual(self.manager.index(), previous)
        self.assertFalse(self.manager.journal.exists())

    def test_crash_recovery_restores_original(self):
        self.manager.save('c')
        index = self.manager.index()
        rollback = 'rollback-' + 'a' * 32
        self.root.rename(self.manager.store / rollback)
        self.root.mkdir()
        self.set_account('b')
        dp.atomic_json(self.manager.journal, {'rollback': rollback, 'indexBefore': index})
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.manager.check()
        self.manager.recover()
        self.assertEqual(self.account(self.root), self.ids['c'][0])
        self.assertFalse(self.manager.journal.exists())

    def test_unsaved_target_is_rejected_and_unregistered_outgoing_is_preserved(self):
        with self.assertRaisesRegex(RuntimeError, 'No saved Desktop profile'):
            self.manager.switch('b')
        self.manager.identity = lambda p: self.ids['b']
        self.manager.switch('b', login=True)
        index = self.manager.index()
        label, saved = next(iter(index['profiles'].items()))
        self.assertTrue(label.startswith('desktop-'))
        self.assertEqual(saved['account'], self.ids['c'][0])
        self.assertTrue(saved['desktopOnly'])
        self.assertIsNone(self.account(self.root))
        self.manager.switch(label)
        self.assertEqual(self.account(self.root), self.ids['c'][0])

    def test_first_switch_saves_unsaved_outgoing_and_allows_return(self):
        self.manager.switch('b', login=True)
        saved = self.manager.index()['profiles']['c']
        snapshot = self.manager.snapshot_path(saved['snapshot']) / 'data'
        self.assertEqual((snapshot / 'Cookies').read_text(), 'encrypted fixture c')
        self.quit.assert_called_once()
        self.set_account('b')
        self.manager.switch('c')
        self.assertIn('b', self.manager.index()['profiles'])
        self.assertEqual(self.account(self.root), self.ids['c'][0])
        self.manager.switch('b')
        self.assertEqual((self.root / 'Cookies').read_text(), 'encrypted fixture b')

    def test_autosave_preview_and_failure_leave_current_account_untouched(self):
        self.manager.switch('b', login=True, dry=True)
        self.assertFalse(self.manager.index_path.exists())
        self.quit.assert_not_called()
        with patch.object(self.manager, 'snapshot', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.manager.switch('b', login=True)
        self.assertEqual(self.account(self.root), self.ids['c'][0])
        self.assertFalse(self.manager.journal.exists())
        self.assertFalse(self.manager.index_path.exists())

    def test_autosave_never_reassigns_an_existing_label(self):
        self.manager.save('c')
        self.set_account('b')
        original = self.manager.index()
        # CLI c now represents b, but c's saved Desktop profile still belongs to c.
        def identity(config):
            if config is not None and config.name.endswith('-c'):
                return self.ids['b']
            raise RuntimeError('No other verified CLI login')
        self.manager.identity = identity
        self.manager.switch('c')
        index = self.manager.index()
        self.assertEqual(index['profiles']['c'], original['profiles']['c'])
        label, saved = next((name, p) for name, p in index['profiles'].items() if name != 'c')
        self.assertTrue(label.startswith('desktop-'))
        self.assertEqual(saved['account'], self.ids['b'][0])
        self.assertEqual(self.account(self.root), self.ids['c'][0])
        self.manager.switch(label)
        self.assertEqual(self.account(self.root), self.ids['b'][0])

    def test_generated_desktop_label_does_not_overwrite_existing_cli_profile(self):
        import hashlib
        base = 'desktop-' + hashlib.sha256(self.ids['c'][0].encode()).hexdigest()[:8]
        (self.home / ('.claude-acct-' + base)).mkdir()
        self.manager.identity = lambda config: self.ids['b']
        self.manager.switch('b', login=True)
        self.assertIn(base + '-2', self.manager.index()['profiles'])
        self.assertNotIn(base, self.manager.index()['profiles'])

    def test_switch_preview_and_lock(self):
        self.save_both()
        previous = self.manager.index()
        self.quit.reset_mock()
        self.manager.switch('c', dry=True)
        self.quit.assert_not_called()
        self.assertEqual(self.manager.index(), previous)
        with self.manager.locked():
            with self.assertRaisesRegex(RuntimeError, 'Another Desktop'):
                with self.manager.locked():
                    self.fail('second lock acquired')


if __name__ == '__main__':
    unittest.main()
