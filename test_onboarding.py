"""All authentication and Desktop lifecycle calls use disposable fixtures."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import desktop_profiles as dp
import onboarding as ob


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / '.claude').mkdir()
        (self.home / '.claude.json').write_text(json.dumps({'projects': {}, 'oauthAccount': {'secret': 'do-not-copy'}}))
        self.ids = {'a': ('a-id', 'a-org', 'a@example.test'), 'd': ('d-id', 'd-org', 'd@example.test')}
        def identity(config):
            return self.ids['a' if config is None else config.name.removeprefix('.claude-acct-')]
        self.manager = dp.Profiles(self.home, identity, None)
        self.manager.root.mkdir(parents=True)
        self.login('a')
        with self.manager.locked():
            pass
        self.addCleanup(patch.stopall)
        patch.object(dp, 'desktop_account', side_effect=lambda root: (root / 'identity').read_text() if (root / 'identity').exists() else None).start()
        self.quit = patch.object(dp, 'quit_app').start()
        self.open = patch.object(dp, 'open_app').start()
        patch.object(dp, 'app_running', return_value=False).start()
        patch('sys.stdout', new_callable=io.StringIO).start()

    def login(self, label):
        (self.manager.root / 'identity').write_text(self.ids[label][0])
        (self.manager.root / 'Cookies').write_text('encrypted fixture for ' + label)

    def test_create_is_private_shares_history_and_does_not_copy_identity(self):
        ob.create(self.manager, 'd')
        profile = self.home / '.claude-acct-d'
        self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
        self.assertTrue((profile / 'projects').is_symlink())
        self.assertEqual(json.loads((profile / '.claude.json').read_text()), {'projects': {}})
        self.assertFalse((profile / '.credentials.json').exists())
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            ob.create(self.manager, 'd')
        self.assertTrue((profile / 'projects').is_symlink())
        self.quit.assert_not_called()

    def test_invalid_names_and_symlinks_never_overwrite(self):
        for name in ('a', '../oops', 'D', '-d', '', 'x'*33):
            with self.assertRaises(RuntimeError):
                ob.create(self.manager, name)
        (self.home / '.claude-acct-link').symlink_to(self.home / 'missing')
        with self.assertRaises(RuntimeError):
            ob.create(self.manager, 'link')
        self.assertTrue((self.home / '.claude-acct-link').is_symlink())

    def test_full_setup_preserves_outgoing_and_can_resume(self):
        ob.create(self.manager, 'd')
        ob.prepare(self.manager, 'd')
        index = self.manager.index()
        self.assertIn('a', index['profiles'])
        self.assertEqual(index['pendingLogin']['label'], 'd')
        snapshot = self.manager.snapshot_path(index['profiles']['a']['snapshot']) / 'data'
        self.assertEqual((snapshot / 'Cookies').read_text(), 'encrypted fixture for a')
        (self.manager.root / 'partial-browser-state').write_text('keep this')
        ob.prepare(self.manager, 'd')
        self.assertTrue((self.manager.root / 'partial-browser-state').exists())
        self.login('d')
        ob.finish(self.manager, 'd')
        self.assertEqual(self.manager.index()['profiles']['d']['account'], 'd-id')
        self.assertNotIn('pendingLogin', self.manager.index())
        self.assertFalse(ob.progress_path(self.manager, 'd').exists())
        self.manager.switch('a')
        self.assertEqual((self.manager.root / 'Cookies').read_text(), 'encrypted fixture for a')
        self.manager.switch('d')
        self.assertEqual((self.manager.root / 'Cookies').read_text(), 'encrypted fixture for d')

    def test_wrong_desktop_and_changed_cli_are_rejected(self):
        ob.create(self.manager, 'd')
        ob.prepare(self.manager, 'd')
        self.login('a')
        with self.assertRaisesRegex(RuntimeError, 'Desktop must be signed into d@'):
            ob.finish(self.manager, 'd')
        self.assertNotIn('d', self.manager.index()['profiles'])
        self.ids['d'] = self.ids['a']
        with self.assertRaisesRegex(RuntimeError, 'CLI account changed'):
            ob.finish(self.manager, 'd')

    def test_finish_flushes_new_login_before_verifying(self):
        ob.create(self.manager, 'd')
        ob.prepare(self.manager, 'd')
        # The browser is signed in, but its new cookie is still only in memory.
        self.assertFalse((self.manager.root / 'identity').exists())
        self.quit.side_effect = lambda: self.login('d')
        self.open.reset_mock()
        with patch.object(dp, 'app_running', return_value=True):
            ob.finish(self.manager, 'd')
        self.assertEqual(self.manager.index()['profiles']['d']['account'], 'd-id')
        self.assertNotIn('pendingLogin', self.manager.index())
        self.open.assert_called()

    def test_failed_finish_reopens_desktop_without_saving_wrong_login(self):
        ob.create(self.manager, 'd')
        ob.prepare(self.manager, 'd')
        self.login('a')
        previous = self.manager.index()
        self.open.reset_mock()
        with patch.object(dp, 'app_running', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'Desktop must be signed into d@'):
                ob.finish(self.manager, 'd')
        self.assertEqual(self.manager.index(), previous)
        self.open.assert_called_once()

    def test_unregistered_outgoing_stays_intact(self):
        ob.create(self.manager, 'd')
        (self.manager.root / 'identity').write_text('unknown-id')
        with self.assertRaisesRegex(RuntimeError, 'no verified CLI profile'):
            ob.prepare(self.manager, 'd')
        self.quit.assert_not_called()
        self.assertEqual((self.manager.root / 'identity').read_text(), 'unknown-id')

    def test_existing_current_account_needs_no_clean_profile(self):
        ob.prepare(self.manager, 'a')
        self.quit.assert_not_called()
        ob.finish(self.manager, 'a')
        self.assertEqual(self.manager.index()['profiles']['a']['account'], 'a-id')

    def test_other_pending_setup_is_preserved(self):
        ob.create(self.manager, 'd')
        ob.prepare(self.manager, 'd')
        with self.assertRaisesRegex(RuntimeError, 'Finish Desktop setup for d first'):
            ob.prepare(self.manager, 'a')
        self.assertEqual(self.manager.index()['pendingLogin']['label'], 'd')

    def test_failed_creation_rolls_back_only_new_directory(self):
        with patch.object(ob.dp, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                ob.create(self.manager, 'd')
        self.assertFalse((self.home / '.claude-acct-d').exists())
        self.assertTrue((self.home / '.claude.json').exists())


if __name__ == '__main__':
    unittest.main()
