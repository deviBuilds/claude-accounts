import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_accounts import Manager, identity, usage_windows
from codex_rpc import RpcError


def jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return 'header.'+encoded+'.fake-signature'


def write_auth(path, account='one', user='user-one', mode='chatgpt'):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'auth_mode': mode, 'tokens': {
        'access_token': jwt({'exp': 4102444800, 'https://api.openai.com/auth': {
            'chatgpt_account_id': account, 'chatgpt_user_id': user}}),
        'id_token': jwt({'email': account+'@example.test', 'sub': user}),
        'refresh_token': 'fake-refresh-secret', 'account_id': account}}
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return data


class FakeRPC:
    def __init__(self, home, calls, settings, **kwargs):
        self.home, self.calls, self.settings = Path(home), calls, settings
        calls.append(('open', str(home), kwargs))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read_account(self, refresh=False):
        self.calls.append(('read', refresh))
        if self.settings.get('error'):
            raise self.settings['error']
        return self.settings.get('metadata', {'account': {'type': 'chatgpt', 'email': 'one@example.test', 'planType': 'plus'}})

    def rate_limits(self):
        self.calls.append(('quota',))
        return self.settings.get('quota', {'accountId': 'one', 'ordinaryUsageAllowed': True,
            'rateLimits': {'primary': {'usedPercent': 21, 'windowDurationMins': 300, 'resetsAt': 4102444800}}})


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.calls, self.settings = [], {}
        factory = lambda home, **kw: FakeRPC(home, self.calls, self.settings, **kw)
        self.manager = Manager(self.home, rpc_factory=factory)
        self.patches = [
            patch('codex_accounts.codex_install.detect_cli', return_value=Path('/fake/codex')),
            patch('codex_accounts.codex_install.detect_desktop', return_value=None),
            patch.object(Manager, 'desktop_state', return_value={'busy': False, 'managed': False}),
            patch('codex_accounts.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'codex-cli 0.166.0\n', '')),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def native_login(self, **kwargs):
        return write_auth(self.manager.native/'auth.json', **kwargs)

    def test_missing_native_login_does_not_create_home_or_start_rpc(self):
        row = self.manager.verify('a')
        self.assertEqual(row['cli_state'], 'missing')
        self.assertFalse(self.manager.native.exists())
        self.assertEqual(self.calls, [])

    def test_new_profile_preserves_an_unregistered_existing_login_directory(self):
        folder = self.manager.store/'profiles'/'work'
        folder.mkdir(parents=True)
        config = folder/'config.toml'
        config.write_text('model = "existing-model"\n')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.manager.create('work')
        self.assertEqual(config.read_text(), 'model = "existing-model"\n')
        self.assertNotIn('work', self.manager.profiles())

    def test_check_remote_quota_cache_and_no_secrets_in_status(self):
        self.native_login()
        row = self.manager.verify('a')
        self.assertEqual(row['cli_state'], 'ready')
        self.assertTrue(row['reusable'])
        self.assertEqual(row['usage']['windows'][0]['percent'], 21)
        self.assertIn(('quota',), self.calls)
        self.manager.verify('a')
        self.assertEqual(self.calls.count(('quota',)), 1)
        status = json.dumps(self.manager.status())
        self.assertNotIn('fake-refresh-secret', status)
        self.assertNotIn('access_token', status)
        self.assertNotIn('fingerprint', status)

    def test_native_keyring_is_detected_without_forcing_file_or_copying_tokens(self):
        self.manager.native.mkdir()
        (self.manager.native/'config.toml').write_text('cli_auth_credentials_store = "keyring"\n')
        row = self.manager.verify('a')
        self.assertEqual(row['cli_state'], 'external')
        self.assertEqual(row['email'], 'one@example.test')
        self.assertFalse(row['reusable'])
        self.assertEqual(self.calls[0][2]['extra_config'], {})
        self.assertFalse((self.manager.native/'auth.json').exists())
        source = self.manager.login_source('a')
        self.assertNotEqual(source, self.manager.native)
        self.assertEqual(self.manager.auth_store('a'), 'file')
        self.assertIn('keyring', (self.manager.native/'config.toml').read_text())

    def test_external_token_mode_never_rotates_or_uses_rpc(self):
        self.native_login(mode='chatgptAuthTokens')
        row = self.manager.verify('a')
        self.assertEqual(row['cli_state'], 'external')
        self.assertFalse(row['reusable'])
        self.assertEqual(self.calls, [])

    def test_quota_identity_mismatch_fails_verification(self):
        self.native_login()
        self.settings['quota'] = {'accountId': 'different', 'rateLimits': {}}
        row = self.manager.verify('a')
        self.assertEqual(row['cli_state'], 'sign_in_required')
        self.assertFalse(row['reusable'])

    def test_auth_failure_keeps_usage_stale_without_marking_login_ready(self):
        self.native_login()
        self.manager.verify('a')
        self.settings['error'] = RpcError('Sign in again.', 'auth')
        row = self.manager.verify('a', force=True)
        self.assertEqual(row['cli_state'], 'sign_in_required')
        self.assertEqual(row['usage']['state'], 'stale')
        self.assertEqual(row['usage']['windows'][0]['percent'], 21)

    def test_identity_drift_stays_blocked_across_repeated_verification(self):
        self.native_login()
        self.manager.verify('a')
        self.native_login(account='two', user='user-two')
        self.settings['quota'] = {'accountId': 'two', 'rateLimits': {}}
        for _ in range(2):
            row = self.manager.verify('a', force=True)
            self.assertEqual(row['cli_state'], 'drift')
            self.assertFalse(row['reusable'])

    def test_activation_links_canonical_auth_and_lock_and_imports_once(self):
        self.native_login()
        with patch('codex_history.import_history', return_value={'copied': 2, 'errors': []}) as imported:
            self.manager.activate('a')
            self.manager.activate('a')
        imported.assert_called_once_with(self.manager.native, self.manager.shared)
        self.assertTrue((self.manager.shared/'auth.json').is_symlink())
        self.assertEqual((self.manager.shared/'auth.json').resolve(), (self.manager.native/'auth.json').resolve())
        self.assertEqual((self.manager.shared/'auth.lock').resolve(), (self.manager.native/'auth.lock').resolve())
        self.assertEqual(self.manager.index()['active'], 'a')

    def test_old_codex_cannot_install_runtime_links(self):
        self.native_login()
        with patch('codex_accounts.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'codex-cli 0.100.0', '')):
            with self.assertRaisesRegex(RuntimeError, '0.166.0'):
                self.manager.activate('a')
        self.assertFalse(self.manager.shared.exists())

    def test_failed_switch_restores_both_links_and_active_identity(self):
        self.native_login()
        self.manager.activate('a')
        original_index = self.manager.index()
        original_links = {name: (self.manager.shared/name).resolve() for name in ('auth.json', 'auth.lock')}
        self.manager.create('work')
        write_auth(self.manager.source('work')/'auth.json')
        original_link = self.manager._link
        calls = []
        def fail_once(name, target):
            calls.append(name)
            if len(calls) == 2:
                raise OSError('simulated failed lock-link update')
            original_link(name, target)
        with patch.object(self.manager, '_link', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.manager.activate('work')
        self.assertEqual(self.manager.index()['active'], 'a')
        self.assertEqual(self.manager.index()['active_identity'], original_index['active_identity'])
        self.assertFalse(self.manager.journal.exists())
        self.assertEqual({name: (self.manager.shared/name).resolve() for name in original_links}, original_links)

    def test_desktop_switch_only_quits_managed_instance(self):
        self.native_login()
        with patch('codex_accounts.codex_install.detect_desktop', return_value=Path('/fake/Codex.app')), \
                patch('codex_desktop.ensure_supported'), patch('codex_desktop.quit_app') as quit_app, \
                patch('codex_desktop.launch_app') as launch:
            self.manager.switch_desktop('a')
        quit_app.assert_called_once_with(self.manager.shared, self.manager.user_data)
        launch.assert_called_once_with(self.manager.shared, self.manager.user_data)

    def test_lease_blocks_switch_and_change_login(self):
        self.native_login()
        with self.manager.lease():
            with self.assertRaisesRegex(RuntimeError, 'CODEX_BUSY'):
                self.manager.activate('a')
            with self.assertRaisesRegex(RuntimeError, 'CODEX_BUSY'):
                self.manager.change_login('a')

    def test_cli_child_holds_lease_and_same_account_sessions_can_coexist(self):
        self.native_login()
        self.manager.activate('a')
        def run(*args, **kwargs):
            self.assertEqual(kwargs['env']['CODEX_HOME'], str(self.manager.shared))
            with self.assertRaisesRegex(RuntimeError, 'CODEX_BUSY'):
                self.manager.activate('a')
            return 0
        with self.manager.lease(), patch('codex_accounts.subprocess.call', side_effect=run):
            self.assertEqual(self.manager.launch_cli('a'), 0)

    def test_new_cli_activation_protects_child_with_shared_lease(self):
        self.native_login()
        def run(*args, **kwargs):
            self.assertEqual(self.manager.index()['active'], 'a')
            with self.assertRaisesRegex(RuntimeError, 'CODEX_BUSY'):
                self.manager.change_login('a')
            return 0
        with patch('codex_accounts.subprocess.call', side_effect=run):
            self.assertEqual(self.manager.launch_cli('a'), 0)

    def test_remove_restore_and_change_login_preserve_canonical_credentials(self):
        original = self.native_login()
        self.manager.activate('a')
        self.manager.remove('a')
        self.assertNotIn('a', self.manager.profiles())
        self.assertIsNone(self.manager.index()['active'])
        self.assertFalse((self.manager.shared/'auth.json').is_symlink())
        self.manager.restore('a')
        self.assertEqual(self.manager.auth('a'), original)
        self.manager.change_login('a')
        self.assertEqual(self.manager.verify('a')['cli_state'], 'missing')
        self.assertEqual(json.loads((self.manager.native/'auth.json').read_text()), original)

    def test_native_login_action_preserves_original_and_uses_managed_vault(self):
        original = self.native_login()
        def login(*args, **kwargs):
            home = Path(kwargs['env']['CODEX_HOME'])
            self.assertNotEqual(home, self.manager.native)
            write_auth(home/'auth.json')
            return 0
        with patch('codex_accounts.subprocess.call', side_effect=login):
            self.assertEqual(self.manager.launch_cli('a', login=True), 0)
        self.assertEqual(json.loads((self.manager.native/'auth.json').read_text()), original)

    def test_save_current_uses_selected_managed_login_when_desktop_is_closed(self):
        original = self.native_login()
        self.manager.create('c')
        managed = self.manager.source('c')
        write_auth(managed/'auth.json', account='two', user='user-two')
        self.settings['quota'] = {'accountId': 'two', 'rateLimits': {}}
        self.settings['metadata'] = {'account': {'type': 'chatgpt', 'email': 'two@example.test', 'planType': 'plus'}}
        self.manager.activate('c')
        self.manager.create('b')
        row = self.manager.save_current('b')
        self.assertEqual(self.manager.source('b'), managed)
        self.assertEqual(row['account'], 'two')
        self.assertEqual(self.manager.index()['active'], 'c')
        self.assertEqual((self.manager.shared/'auth.json').resolve(), (managed/'auth.json').resolve())
        self.assertEqual(json.loads((self.manager.native/'auth.json').read_text()), original)

    def test_save_current_falls_back_to_native_without_matching_runtime_login(self):
        self.native_login()
        self.manager.activate('a')
        (self.manager.shared/'auth.json').unlink()
        self.manager.create('b')
        row = self.manager.save_current('b')
        self.assertEqual(self.manager.source('b'), self.manager.native)
        self.assertEqual(row['account'], 'one')

    def test_label_path_and_symlink_conflicts_are_rejected(self):
        for label in ('../escape', '', 'UPPER', 'a/b'):
            with self.assertRaises(RuntimeError):
                self.manager.create(label)
        self.manager.store.mkdir()
        (self.manager.store/'profiles').symlink_to(self.home/'outside')
        with self.assertRaises(RuntimeError):
            self.manager.create('work')
        self.assertFalse((self.home/'outside').exists())


class UsageTests(unittest.TestCase):
    def test_permission_and_spend_limit_override_low_percentages(self):
        base = {'rateLimits': {'primary': {'usedPercent': 0, 'windowDurationMins': 300}}}
        self.assertFalse(usage_windows(base)[1])
        self.assertTrue(usage_windows({**base, 'ordinaryUsageAllowed': False})[1])
        self.assertTrue(usage_windows({'rateLimits': {'spendControlReached': True}})[1])

    def test_malformed_tokens_and_windows_are_not_ready(self):
        self.assertEqual(identity({'tokens': 'bad'})['kind'], 'missing')
        windows, limited = usage_windows({'rateLimits': {'primary': {'usedPercent': float('nan')}}})
        self.assertEqual(windows, [])
        self.assertFalse(limited)


if __name__ == '__main__':
    unittest.main()
