from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

import cli_import as ci
import desktop_profiles as dp

ACCOUNT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
ORG = '33333333-3333-4333-8333-333333333333'


def profile(account=ACCOUNT, email='one@example.test'):
    return {'account': {'uuid': account, 'email': email, 'has_claude_max': True, 'full_name': 'Correct Name'},
            'organization': {'uuid': ORG, 'rate_limit_tier': 'test-tier'}, 'application': {'uuid': ci.CLIENT_ID}}


class MemoryKeychain:
    def __init__(self, raw=None):
        self.raw, self.writes, self.closed = raw, [], False
    def read(self): return self.raw
    def write(self, value):
        self.writes.append(value)
        self.raw = value
    def close(self): self.closed = True


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = self.home / '.claude-desktop-profiles'
        self.store.mkdir()
        self.index = {'profiles': {'b': {**ci.identity(profile()), 'snapshot': 'unchanged'}}}
        dp.atomic_json(self.store / 'index.json', self.index)
        directory = self.home / '.claude-acct-b'
        directory.mkdir()
        self.config = directory / '.claude.json'
        dp.atomic_json(self.config, {'oauthAccount': {'accountUuid': OTHER, 'emailAddress': 'wrong@example.test',
                                                    'staleOnly': True}, 'projects': {'keep': True}, 'theme': 'dark'})
        self.path = self.home / 'login éxport (1).json'
        self.export = {'type': 'claude', 'disabled': False, 'access_token': 'FAKE-import-token',
                       'refresh_token': 'FAKE-refresh-token', 'account_uuid': ACCOUNT, 'organization_uuid': ORG,
                       'email': 'one@example.test', 'expired': datetime.fromtimestamp(time.time()+3600, timezone.utc).isoformat()}
        self.save_export()
        self.keychain = MemoryKeychain(json.dumps({'claudeAiOauth': {'accessToken': '', 'refreshToken': ''}, 'keep': 123}).encode())
        self.before = self.keychain.raw
        self.verifier = lambda token: profile()

    def save_export(self): self.path.write_text(json.dumps(self.export))

    def run_import(self, signature=None, **kwargs):
        return ci.import_login(self.home, 'b', self.path, signature, verifier=self.verifier,
                               storage=lambda home, label: self.keychain, **kwargs)

    def test_verified_preview_then_import_repairs_cache_and_preserves_other_data(self):
        before_config = self.config.read_bytes()
        index_before = (self.store / 'index.json').read_bytes()
        review = self.run_import()
        self.assertTrue(review['cache_repaired'])
        self.assertEqual(review['source'], 'saved Desktop profile')
        self.assertNotIn('token', json.dumps(review))
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.keychain.writes, [])
        self.run_import(review['signature'])
        saved = json.loads(self.keychain.raw)
        self.assertEqual(saved['keep'], 123)
        self.assertEqual(saved['claudeAiOauth']['accessToken'], self.export['access_token'])
        self.assertEqual(saved['claudeAiOauth']['subscriptionType'], 'max')
        self.assertIn('user:inference', saved['claudeAiOauth']['scopes'])
        updated = json.loads(self.config.read_bytes())
        self.assertEqual(updated['projects'], {'keep': True})
        self.assertEqual(updated['theme'], 'dark')
        self.assertEqual(updated['oauthAccount']['accountUuid'], ACCOUNT)
        self.assertEqual(updated['oauthAccount']['emailAddress'], 'one@example.test')
        self.assertNotIn('staleOnly', updated['oauthAccount'])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.store / 'index.json').read_bytes(), index_before)

    def test_wrong_server_account_or_export_claims_never_writes(self):
        for change in ('server', 'metadata', 'organization', 'application'):
            with self.subTest(change=change):
                result = profile()
                exported = dict(self.export)
                if change == 'server': result = profile(OTHER, 'wrong@example.test')
                if change == 'metadata': exported['email'] = 'wrong@example.test'
                if change == 'organization': result['organization']['uuid'] = OTHER
                if change == 'application': result['application']['uuid'] = OTHER
                self.path.write_text(json.dumps(exported))
                self.verifier = lambda token: result
                with self.assertRaises(ci.ImportError): self.run_import()
                self.assertEqual(self.keychain.writes, [])

    def test_profile_binding_rejects_an_honest_export_from_another_account(self):
        self.export.update(account_uuid=OTHER, email='wrong@example.test')
        self.save_export()
        self.verifier = lambda token: profile(OTHER, 'wrong@example.test')
        with self.assertRaisesRegex(ci.ImportError, 'saved Desktop profile'):
            self.run_import()
        self.assertFalse(self.keychain.writes)

    def test_native_format_and_path_with_spaces_quotes_unicode(self):
        for name in (str(self.path), "'"+str(self.path)+"'", str(self.path).replace(' ', r'\ ')):
            self.assertEqual(ci.normalize_path(name), self.path)
        self.path.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'FAKE-a', 'refreshToken': 'FAKE-r',
                                                         'expiresAt': int((time.time()+3600)*1000), 'scopes': ci.SCOPES}}))
        self.assertEqual(self.run_import()['email'], 'one@example.test')

    def test_invalid_inputs_and_expired_export_are_rejected(self):
        invalid = [[], {}, {**self.export, 'disabled': True}, {**self.export, 'refresh_token': ''},
                   {**self.export, 'expired': '2000-01-01T00:00:00Z'}, {**self.export, 'expired': '2030-01-01'},
                   {'claudeAiOauth': {'accessToken': 'a', 'refreshToken': 'r', 'expiresAt': float('inf')}},
                   {'claudeAiOauth': {'accessToken': 'a', 'refreshToken': 'r', 'expiresAt': (time.time()+3600)*1000, 'scopes': []}}]
        for data in invalid:
            with self.subTest(data_type=type(data).__name__):
                self.path.write_text(json.dumps(data))
                with self.assertRaises(ci.ImportError): self.run_import()
        self.path.write_text('invalid JSON with FAKE-secret')
        with self.assertRaises(ci.ImportError): self.run_import()
        self.path.write_bytes(b' ' * (ci.MAX_FILE + 1))
        with self.assertRaises(ci.ImportError): self.run_import()
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaisesRegex(ci.ImportError, 'regular'): self.run_import()
        self.assertFalse(self.keychain.writes)

    def test_changes_since_preview_are_rejected(self):
        review = self.run_import()
        self.export['refresh_token'] = 'FAKE-changed'
        self.save_export()
        with self.assertRaisesRegex(ci.ImportError, 'changed after verification'):
            self.run_import(review['signature'])
        self.assertFalse(self.keychain.writes)

    def test_profile_and_keychain_changes_since_preview_are_rejected(self):
        review = self.run_import()
        self.keychain.raw = json.dumps({'other': True}).encode()
        with self.assertRaisesRegex(ci.ImportError, 'changed after verification'):
            self.run_import(review['signature'])
        self.keychain.raw = self.before
        self.index['profiles']['b']['snapshot'] = 'changed'
        dp.atomic_json(self.store / 'index.json', self.index)
        with self.assertRaisesRegex(ci.ImportError, 'changed after verification'):
            self.run_import(review['signature'])
        self.assertFalse(self.keychain.writes)

    def test_change_while_waiting_for_keychain_permission_never_writes(self):
        review = self.run_import()
        reads = iter([self.before, b'{"concurrent": true}'])
        with patch.object(self.keychain, 'read', side_effect=lambda: next(reads)):
            with self.assertRaisesRegex(ci.ImportError, 'changed while verifying'):
                self.run_import(review['signature'])
        self.assertFalse(self.keychain.writes)

    def test_keychain_failure_does_not_change_cache(self):
        review = self.run_import()
        before_config = self.config.read_bytes()
        with patch.object(self.keychain, 'write', side_effect=ci.ImportError('Permission denied')):
            with self.assertRaises(ci.ImportError): self.run_import(review['signature'])
        self.assertEqual(self.config.read_bytes(), before_config)

    def test_failed_cache_write_rolls_back_existing_and_new_credentials(self):
        for before in (self.before, None):
            self.keychain.raw = before
            review = self.run_import()
            with self.assertRaisesRegex(ci.ImportError, 'previous Keychain login was restored'):
                self.run_import(review['signature'], writer=lambda *args: (_ for _ in ()).throw(OSError('disk full')))
            self.assertEqual(self.keychain.raw, before)

    def test_current_verified_cli_outranks_wrong_cache_without_desktop(self):
        dp.atomic_json(self.store / 'index.json', {'profiles': {}})
        self.keychain.raw = json.dumps({'claudeAiOauth': {'accessToken': 'FAKE-existing', 'expiresAt': (time.time()+3600)*1000}}).encode()
        review = self.run_import()
        self.assertEqual(review['source'], 'current CLI login')
        self.run_import(review['signature'])
        self.assertEqual(json.loads(self.config.read_bytes())['oauthAccount']['accountUuid'], ACCOUNT)

    def test_unbound_profile_and_pinned_setup(self):
        dp.atomic_json(self.store / 'index.json', {'profiles': {}})
        dp.atomic_json(self.config, {})
        self.assertEqual(self.run_import()['source'], 'new profile')
        dp.atomic_json(self.store / 'onboarding-b.json', ci.identity(profile(OTHER, 'other@example.test')))
        with self.assertRaisesRegex(ci.ImportError, 'verified setup identity'): self.run_import()

    def test_cli_only_missing_login_uses_cache_as_constraint(self):
        dp.atomic_json(self.store / 'index.json', {'profiles': {}})
        with self.assertRaisesRegex(ci.ImportError, 'cached profile identity'): self.run_import()
        dp.atomic_json(self.config, {'oauthAccount': ci.cached_identity(profile())})
        self.assertIn('cached profile', self.run_import()['source'])

    def test_revoked_cli_can_be_restored_but_network_failure_cannot_bypass_identity(self):
        dp.atomic_json(self.store / 'index.json', {'profiles': {}})
        dp.atomic_json(self.config, {'oauthAccount': ci.cached_identity(profile())})
        self.keychain.raw = json.dumps({'claudeAiOauth': {'accessToken': 'revoked', 'expiresAt': (time.time()+3600)*1000}}).encode()
        def revoked(token):
            if token == 'revoked': raise ci.RejectedLogin('Rejected')
            return profile()
        self.verifier = revoked
        self.assertIn('cached profile', self.run_import()['source'])
        def offline(token):
            if token == 'revoked': raise ci.ImportError('Network unavailable')
            return profile()
        self.verifier = offline
        with self.assertRaisesRegex(ci.ImportError, 'Network unavailable'): self.run_import()
        self.assertFalse(self.keychain.writes)

    def test_refresh_lock_blocks_import(self):
        import fcntl
        root = self.home / '.claude-account-refresh'
        root.mkdir()
        with (root / 'b.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ci.ImportError, 'refreshing'): self.run_import()
        self.assertFalse(self.keychain.writes)

    def test_removed_profile_and_interrupted_switch_are_rejected(self):
        self.index['removedProfiles'] = {'b': {}}
        dp.atomic_json(self.store / 'index.json', self.index)
        with self.assertRaisesRegex(ci.ImportError, 'restore'): self.run_import()
        del self.index['removedProfiles']
        dp.atomic_json(self.store / 'index.json', self.index)
        (self.store / 'pending-switch.json').write_text('{}')
        with self.assertRaisesRegex(ci.ImportError, 'Recover'): self.run_import()
        self.assertFalse(self.keychain.writes)

    def test_profile_and_file_symlink_safety(self):
        target = self.home / 'shared.json'
        self.config.rename(target)
        self.config.symlink_to(target)
        with self.assertRaisesRegex(ci.ImportError, 'symlinks'): self.run_import()
        self.assertFalse(self.keychain.writes)

    def test_default_profile_writes_home_identity_and_default_keychain_service(self):
        captured = []
        def storage(home, label):
            captured.append(label)
            return self.keychain
        dp.atomic_json(self.home / '.claude.json', {})
        review = ci.import_login(self.home, 'a', self.path, verifier=self.verifier, storage=storage)
        ci.import_login(self.home, 'a', self.path, review['signature'], verifier=self.verifier, storage=storage)
        self.assertEqual(captured, ['a', 'a'])
        self.assertEqual(json.loads((self.home / '.claude.json').read_bytes())['oauthAccount']['emailAddress'], 'one@example.test')
        self.assertFalse((self.home / '.claude' / '.claude.json').exists())

    def test_helper_never_prints_unexpected_error_or_json_payload(self):
        output = io.StringIO()
        with patch('sys.stderr', output), patch.object(ci, 'import_login', side_effect=ValueError('FAKE-secret-access-token')):
            self.assertEqual(ci.main(['b', str(self.path)]), 1)
        self.assertNotIn('FAKE-secret', output.getvalue())

    def test_http_rejection_and_outage_do_not_leak_response(self):
        for status in (401, 403, 429, 500):
            error = urllib.error.HTTPError('https://api.anthropic.com', status, 'FAKE-secret', {}, io.BytesIO(b'FAKE-secret'))
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(ci.ImportError) as caught: ci.verify_token('FAKE-access')
                self.assertNotIn('FAKE-secret', str(caught.exception))
                self.assertEqual(isinstance(caught.exception, ci.RejectedLogin), status in (401, 403))


if __name__ == '__main__':
    unittest.main()
