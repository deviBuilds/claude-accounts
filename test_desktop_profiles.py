"""Transaction tests use disposable fixture profiles and never control the real app."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import desktop_profiles as dp


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

    def test_unsaved_target_and_unverified_outgoing_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'No saved Desktop profile'):
            self.manager.switch('b')
        self.manager.identity = lambda p: self.ids['b']
        with self.assertRaisesRegex(RuntimeError, 'no verified CLI profile'):
            self.manager.switch('b', login=True)
        self.quit.assert_not_called()

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
        with self.assertRaisesRegex(RuntimeError, 'no verified CLI profile'):
            self.manager.switch('c')
        self.assertEqual(self.manager.index(), original)
        self.assertEqual(self.account(self.root), self.ids['b'][0])

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
