"""Identity changes use fixture accounts; no real auth or Desktop control."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import account_status
import desktop_profiles as dp
import onboarding
import profile_management as pm


class ManagementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.identities = {
            'a': ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'org-a', 'a@example.test'),
            'c': ('cccccccc-cccc-cccc-cccc-cccccccccccc', 'org-c', 'c@example.test')}
        (self.home / '.claude-acct-c').mkdir()
        self.manager = dp.Profiles(self.home, self.identity, None)
        self.manager.root.mkdir(parents=True)
        with self.manager.locked():
            pass
        def account(root):
            config = root / 'config.json'
            return json.loads(config.read_text())['account'] if config.exists() else None
        self.account = account
        self.addCleanup(patch.stopall)
        patch.object(dp, 'desktop_account', side_effect=account).start()
        patch.object(dp, 'app_running', return_value=False).start()
        patch.object(dp, 'desktop_state', return_value={'running': False, 'background': False}).start()
        self.quit = patch.object(dp, 'quit_app').start()
        self.open = patch.object(dp, 'open_app').start()
        patch('sys.stdout', new_callable=io.StringIO).start()
        for label in ('a', 'c'):
            self.sign_desktop(label)
            self.manager.save(label)
        self.quit.reset_mock()

    def identity(self, config):
        return self.identities['a' if config is None else config.name.removeprefix('.claude-acct-')]

    def sign_desktop(self, label):
        (self.manager.root / 'config.json').write_text(json.dumps({'account': self.identities[label][0]}))

    def swap_cli(self):
        self.identities['a'], self.identities['c'] = self.identities['c'], self.identities['a']

    def plan(self):
        return pm.repair_plan(self.manager.index(), pm.verified_identities(self.manager))

    def test_swap_is_atomic_and_does_not_touch_active_desktop_or_cli(self):
        before = self.manager.index()
        root_before = (self.manager.root / 'config.json').read_bytes()
        self.swap_cli()
        plan = self.plan()
        self.assertEqual([c['label'] for c in plan['changes']], ['a', 'c'])
        pm.reconcile(self.manager, 'a', plan['signature'])
        after = self.manager.index()
        self.assertEqual(after['profiles']['a']['snapshot'], before['profiles']['c']['snapshot'])
        self.assertEqual(after['profiles']['c']['snapshot'], before['profiles']['a']['snapshot'])
        self.assertEqual(len(after['bindingHistory']), 2)
        self.assertEqual((self.manager.root / 'config.json').read_bytes(), root_before)
        self.quit.assert_not_called()
        self.assertFalse(self.plan()['changes'])

    def test_repair_rejects_changed_identity_since_preview(self):
        self.swap_cli()
        plan = self.plan()
        self.swap_cli()
        before = self.manager.index()
        with self.assertRaisesRegex(RuntimeError, 'state changed'):
            pm.reconcile(self.manager, 'a', plan['signature'])
        self.assertEqual(self.manager.index(), before)

    def test_invalid_snapshot_aborts_entire_swap(self):
        self.swap_cli()
        plan = self.plan()
        before = self.manager.index()
        with patch.object(dp, 'desktop_account', return_value='wrong'):
            with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
                pm.reconcile(self.manager, 'a', plan['signature'])
        self.assertEqual(self.manager.index(), before)

    def test_failed_index_write_preserves_both_bindings(self):
        self.swap_cli()
        before = self.manager.index()
        plan = self.plan()
        with patch.object(dp, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                pm.reconcile(self.manager, 'a', plan['signature'])
        self.assertEqual(self.manager.index(), before)

    def test_status_shows_both_identities_and_correct_active_desktop(self):
        self.swap_cli()
        status = account_status.collect(self.home, lambda _: {'accessToken': 'fixture'}, self.identity)
        rows = {r['label']: r for r in status['accounts']}
        self.assertEqual(rows['a']['cli_email'], 'c@example.test')
        self.assertEqual(rows['a']['desktop_email'], 'a@example.test')
        self.assertTrue(rows['a']['identity_drift'])
        self.assertFalse(rows['a']['desktop_active'])
        self.assertTrue(rows['c']['desktop_active'])
        self.assertEqual(status['reconcile_plan'], self.plan())
        self.assertNotIn('fixture', json.dumps(status))

    def test_remove_and_restore_default_profile_keeps_data_and_reserved_label(self):
        before = self.manager.index()['profiles']['a']
        pm.remove(self.manager, 'a')
        self.assertNotIn('a', account_status.discover(self.home, self.manager.index()))
        self.assertTrue(self.manager.snapshot_path(before['snapshot']).is_dir())
        with self.assertRaisesRegex(RuntimeError, 'removed'):
            onboarding.create(self.manager, 'a')
        with self.assertRaisesRegex(RuntimeError, 'removed'):
            self.manager.switch('a')
        pm.restore(self.manager, 'a')
        self.assertEqual(self.manager.index()['profiles']['a'], before)
        self.assertIn('a', account_status.discover(self.home, self.manager.index()))

    def test_remove_preserves_shared_symlinks_and_cli_configuration(self):
        shared = self.home / '.claude' / 'history.jsonl'
        shared.parent.mkdir()
        shared.write_text('shared history')
        link = self.home / '.claude-acct-c' / 'history.jsonl'
        link.symlink_to(shared)
        pm.remove(self.manager, 'c')
        self.assertEqual(link.read_text(), 'shared history')
        self.assertTrue(link.is_symlink())
        with self.assertRaisesRegex(RuntimeError, 'removed'):
            onboarding.create(self.manager, 'c')
        pm.restore(self.manager, 'c')
        self.assertTrue(link.is_symlink())

    def test_switch_preserves_removed_outgoing_without_resurrecting_it(self):
        pm.remove(self.manager, 'c')  # c is currently in Desktop.
        self.manager.switch('a')
        self.assertNotIn('c', account_status.discover(self.home, self.manager.index()))
        self.assertNotIn('c', self.manager.index()['profiles'])
        self.assertIsNotNone(self.manager.index()['removedProfiles']['c']['profile']['snapshot'])

    def test_change_desktop_retains_old_binding_until_save(self):
        old = self.manager.index()['profiles']['a']
        pm.begin_desktop_change(self.manager, 'a')
        self.assertIsNone(self.account(self.manager.root))
        self.assertEqual(self.manager.index()['profiles']['a'], old)
        self.assertTrue(self.manager.index()['pendingLogin']['replaceDesktop'])
        self.sign_desktop('c')
        pm.finish_desktop_change(self.manager, 'a')
        index = self.manager.index()
        self.assertEqual(index['profiles']['a']['account'], self.identities['c'][0])
        self.assertTrue(any(p['snapshot'] == old['snapshot'] for p in index['bindingHistory']))
        self.assertNotIn('pendingLogin', index)
        self.assertEqual(self.identities['a'][2], 'a@example.test')

    def test_desktop_to_cli_requires_the_chosen_cli_identity(self):
        pm.begin_desktop_change(self.manager, 'a', cli_target=True)
        self.sign_desktop('c')
        before = self.manager.index()
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            pm.finish_desktop_change(self.manager, 'a')
        self.assertEqual(self.manager.index(), before)
        self.sign_desktop('a')
        pm.finish_desktop_change(self.manager, 'a')
        self.assertNotIn('pendingLogin', self.manager.index())

    def test_busy_desktop_change_does_not_replace_or_reopen(self):
        pm.begin_desktop_change(self.manager, 'a')
        self.sign_desktop('c')
        before = self.manager.index()
        self.open.reset_mock()
        with patch.object(dp, 'quit_app', side_effect=dp.DesktopBusyError('busy')):
            with self.assertRaises(dp.DesktopBusyError):
                pm.finish_desktop_change(self.manager, 'a')
        self.assertEqual(self.manager.index(), before)
        self.open.assert_not_called()

    def test_cancel_desktop_change_preserves_old_login_and_current_app(self):
        pm.begin_desktop_change(self.manager, 'a')
        self.sign_desktop('c')
        before = self.manager.index()['profiles']
        pm.cancel_desktop_change(self.manager, 'a')
        self.assertEqual(self.manager.index()['profiles'], before)
        self.assertEqual(self.account(self.manager.root), self.identities['c'][0])
        self.assertNotIn('pendingLogin', self.manager.index())

    def test_pending_change_blocks_remove_repair_and_cli_change(self):
        pm.begin_desktop_change(self.manager, 'a')
        for fn, args in ((pm.remove, ()), (pm.reset_cli_setup, ()), (pm.reconcile, ('ignored',))):
            with self.assertRaises(RuntimeError):
                fn(self.manager, 'a', *args)
        self.assertTrue(self.manager.index()['pendingLogin'])

    def test_cli_change_clears_old_setup_pin_but_keeps_desktop_snapshot(self):
        path = self.manager.store / 'onboarding-a.json'
        path.write_text(json.dumps({'account': 'old'}))
        before = self.manager.index()
        pm.reset_cli_setup(self.manager, 'a')
        self.assertFalse(path.exists())
        self.assertEqual(self.manager.index(), before)


class ManagementTuiTests(unittest.TestCase):
    def setUp(self):
        import accounts_tui as tui
        from test_accounts_tui import Screen
        self.tui = tui
        patch('curses.has_colors', return_value=False).start()
        patch('curses.curs_set').start()
        self.addCleanup(patch.stopall)
        self.screen = Screen()
        self.app = tui.App(self.screen, demo=True)
        row = self.app.state['accounts'][0]
        row.update(cli_state='ready', cli_email='new@example.test', email='new@example.test',
                   cli_account='new-id', desktop_email='old@example.test', desktop_account='old-id',
                   desktop_active=True, identity_drift=True)
        self.app.state['active_email'] = 'old@example.test'
        self.app.state['reconcile_plan'] = {'signature': 'preview-hash', 'changes': [
            {'label': 'a', 'before': {'account': 'old-id', 'email': 'old@example.test'},
             'after': {'account': 'new-id', 'email': 'new@example.test'}},
            {'label': 'c', 'before': {'account': 'new-id', 'email': 'new@example.test'},
             'after': {'account': 'old-id', 'email': 'old@example.test'}}]}
        self.app.dispatch('account:a')

    def test_mismatch_menu_previews_every_affected_profile_and_cancels(self):
        self.assertEqual(self.app.items()[0][0], 'reconcile_menu')
        self.assertNotIn('switch', [r[0] for r in self.app.items()])
        self.assertNotIn('save', [r[0] for r in self.app.items()])
        self.assertNotIn('Already selected in Desktop.', self.app.header()[1])
        self.app.dispatch('reconcile_menu')
        self.app.draw()
        self.assertIn('CLI: new@example.test', self.screen.text)
        self.app.dispatch('repair_bindings')
        self.assertEqual(self.app.modal[1], 'manage:reconcile:preview-hash')
        self.assertIn('a:', self.app.modal[3][0])
        self.assertIn('c:', self.app.modal[3][1])
        self.app.key(27)
        self.assertIsNone(self.app.worker)
        self.app.dispatch('back')
        self.assertEqual(self.app.page, 'account')

    def test_header_uses_desktop_identity_not_mismatching_cli_email(self):
        self.app.draw()
        self.assertIn('old@example.test', self.screen.text)
        self.app.goto('home')
        self.app.draw()
        self.assertTrue(any('LOGIN MISMATCH' in x for x in self.screen.text))
        self.assertTrue(any('CLI: new@example.test' in x for x in self.screen.text))
        self.assertTrue(any('Desktop: old@example.test' in x for x in self.screen.text))

    def test_remove_and_restore_are_explicit_and_do_not_execute_in_demo(self):
        self.app.dispatch('manage')
        self.app.dispatch('remove_profile')
        self.assertIn('Neither app is signed out', ' '.join(self.app.modal[3]))
        self.app.key(10)
        self.assertEqual(self.app.modal[2], 'Demo mode')
        self.app.key(27)
        self.app.state['removed_profiles'] = ['c']
        self.app.dispatch('removed')
        self.assertEqual(self.app.items()[0][0], 'restore:c')
        self.app.dispatch('restore:c')
        self.assertEqual(self.app.modal[1], 'manage:restore')

    def test_change_cli_uses_target_desktop_email_only_when_requested(self):
        self.app.dispatch('cli_to_desktop')
        self.assertEqual(self.app.login_hint, 'old@example.test')
        self.app.key(27)
        self.app.dispatch('change_cli')
        self.assertEqual(self.app.login_hint, '')
        self.assertEqual(self.app.modal[1], 'manage:reset-cli-setup')

    def test_repair_worker_passes_preview_signature_to_backend(self):
        self.app.demo = False
        with patch.object(self.tui, 'Worker') as worker:
            self.app.start('manage:reconcile:preview-hash')
            self.assertEqual(worker.call_args.args[0][-4:], ['reconcile', 'a', '--expected', 'preview-hash'])
            self.assertEqual(self.app.job, 'manage:reconcile')

    def test_desktop_change_success_and_busy_retry_keep_the_change_page(self):
        from types import SimpleNamespace
        import queue
        events = queue.Queue()
        events.put(('done', 0))
        self.app.worker = SimpleNamespace(events=events)
        self.app.job = 'manage:begin-desktop-change'
        with patch.object(self.app, 'refresh'):
            self.app.poll()
        self.assertEqual(self.app.page, 'desktop_change')
        events = queue.Queue()
        events.put(('line', 'DESKTOP_BUSY: Could not stop a task'))
        events.put(('done', 1))
        self.app.worker = SimpleNamespace(events=events)
        self.app.job = 'manage:finish-desktop-change'
        with patch.object(self.tui.dp, 'desktop_state', return_value={'running': False, 'background': True}):
            self.app.poll()
        self.assertEqual(self.app.page, 'desktop_change')
        self.assertEqual(self.app.modal[1], 'retry_save:manage:finish-desktop-change')

    def test_all_management_pages_render_in_short_terminal(self):
        self.screen.size = (20, 60)
        for page in ('manage', 'reconcile', 'removed', 'desktop_change'):
            self.app.goto(page)
            self.app.draw()
            self.assertTrue(any('Enter:' in text for text in self.screen.text))


if __name__ == '__main__':
    unittest.main()
