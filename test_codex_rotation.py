import copy
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

import codex_rotation as rotation
from desktop_profiles import atomic_json


class FakeManager:
    def __init__(self, home):
        self.home = home
        self.shared = home/'.codex-rotate'
        self.shared.mkdir()
        self.active = None
        self.busy = False
        self.activations = []
        self.checks = []
        self.rows = {label: {'label': label, 'account': 'account-'+label, 'user': 'user-'+label,
                            'email': label+'@example.test', 'kind': 'chatgpt', 'reusable': True,
                            'cli_state': 'ready', 'cli_detail': 'Verified', 'limited': False,
                            'usage': {'state': 'ready', 'windows': [{'key': 'session', 'percent': 25}]}}
                     for label in ('a', 'b', 'c')}

    def verify(self, label, force=False):
        self.checks.append((label, force))
        row = self.rows[label]
        if isinstance(row, Exception):
            raise row
        return copy.deepcopy(row)

    def index(self):
        return {'active': self.active}

    def profiles(self):
        return {label: {} for label in self.rows}

    def activate(self, label, verified=None):
        if self.busy:
            raise RuntimeError('CODEX_BUSY: managed Codex app-server is still running')
        self.active = label
        self.activations.append(label)
        vault = self.home/('vault-'+label)
        vault.write_text('opaque credential')
        link = self.shared/'auth.json'
        link.unlink(missing_ok=True)
        link.symlink_to(vault)

    def launch_cli(self, label, extra):
        return label, extra


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-rotation-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.manager = FakeManager(self.home)
        self.rotation = rotation.Rotation(manager=self.manager)
        self.run = patch.object(rotation.subprocess, 'run', return_value=SimpleNamespace(returncode=0)).start()
        self.addCleanup(patch.stopall)

    def enable(self):
        self.rotation.enable(['a', 'b'])
        return self.rotation.tick()

    def test_limit_rotates_only_after_runtime_is_idle(self):
        self.assertEqual(self.enable()['active'], 'a')
        history = self.manager.shared/'history.jsonl'
        history.write_text('history retained\n')
        self.manager.rows['a']['limited'] = True
        self.manager.rows['a']['usage']['windows'][0]['percent'] = 100
        self.manager.busy = True
        state = self.rotation.tick()
        self.assertEqual((state['active'], state['pending'], state['state']), ('a', 'b', 'waiting_idle'))
        self.assertIn('even after a reply', state['detail'])
        self.assertEqual(self.manager.activations, ['a'])
        self.manager.busy = False
        state = self.rotation.tick()
        self.assertEqual((state['active'], state['pending'], state['state']), ('b', None, 'ready'))
        self.assertEqual(history.read_text(), 'history retained\n')

    def test_auth_failure_rotates_and_is_not_misreported_as_identity_change(self):
        self.enable()
        self.manager.rows['a'] = {'cli_state': 'sign_in_required', 'cli_detail': 'Session expired'}
        state = self.rotation.tick()
        self.assertEqual(state['active'], 'b')
        self.assertEqual(state['profiles'][0]['state'], 'auth')

    def test_transient_unknown_and_stale_usage_hold_current_login(self):
        self.enable()
        for result in (RuntimeError('network unavailable'),
                       {**self.manager.rows['a'], 'usage': {'state': 'stale', 'detail': 'Rate limited', 'windows': []}}):
            self.manager.rows['a'] = result
            state = self.rotation.tick()
            self.assertEqual((state['active'], state['state']), ('a', 'unknown'))
            self.assertEqual(self.manager.activations, ['a'])

    def test_verified_identity_drift_is_ineligible(self):
        self.enable()
        self.manager.rows['a']['account'] = 'different-account'
        state = self.rotation.tick()
        self.assertEqual(state['active'], 'b')
        self.assertEqual(state['profiles'][0]['state'], 'changed')

    def test_native_transient_failures_hold_but_native_limit_rotates(self):
        self.enable()
        for kind in ('unavailable', 'rate_limited'):
            self.manager.rows['a'] = {'cli_state': 'unverified', 'reusable': False,
                                      'error_kind': kind, 'cli_detail': 'Check unavailable',
                                      'usage': {'state': 'unavailable', 'windows': []}}
            state = self.rotation.tick()
            self.assertEqual((state['active'], state['state']), ('a', 'unknown'))
            self.assertEqual(self.manager.activations, ['a'])
        self.manager.rows['a']['error_kind'] = 'limited'
        state = self.rotation.tick()
        self.assertEqual((state['active'], state['state']), ('b', 'ready'))
        self.assertEqual(state['profiles'][0]['state'], 'limited')

    def test_removed_active_profile_does_not_hold_remaining_valid_login(self):
        self.enable()
        del self.manager.rows['a']
        self.manager.active = None
        (self.manager.shared/'auth.json').unlink()
        state = self.rotation.tick()
        self.assertEqual((state['active'], state['state']), ('b', 'ready'))
        self.assertEqual(state['profiles'][0]['state'], 'removed')
        self.assertEqual(self.manager.activations, ['a', 'b'])

    def test_schedule_is_five_minutes_and_checks_respect_manager_cache(self):
        self.enable()
        spec = plistlib.loads(self.rotation.agent_path().read_bytes())
        self.assertEqual(spec['StartInterval'], 300)
        self.assertTrue(spec['RunAtLoad'])
        self.assertTrue(all(not force for _, force in self.manager.checks))
        state = self.rotation.status()
        self.assertEqual(state['poll_seconds'], 300)
        with patch.object(rotation.time, 'time', return_value=state['checked_at']+301):
            self.assertFalse(self.rotation.status()['stale'])

    def test_disable_keeps_logins_and_history(self):
        self.enable()
        history = self.manager.shared/'history.jsonl'
        history.write_text('history')
        before = (self.manager.shared/'auth.json').readlink()
        self.rotation.disable()
        self.assertFalse(self.rotation.tick()['enabled'])
        self.assertEqual((self.manager.shared/'auth.json').readlink(), before)
        self.assertEqual(history.read_text(), 'history')
        self.assertFalse(self.rotation.agent_path().exists())

    def test_duplicate_and_external_credentials_cannot_enable(self):
        self.manager.rows['b']['account'] = self.manager.rows['a']['account']
        self.manager.rows['b']['user'] = self.manager.rows['a']['user']
        with self.assertRaisesRegex(RuntimeError, 'distinct'):
            self.rotation.enable(['a', 'b'])
        self.manager.rows['b']['account'] = 'account-b'
        for change in ({'reusable': False}, {'cli_state': 'external'}, {'kind': 'apikey'}):
            original = copy.deepcopy(self.manager.rows['b'])
            self.manager.rows['b'].update(change)
            with self.assertRaisesRegex(RuntimeError, 'Verify'):
                self.rotation.enable(['a', 'b'])
            self.manager.rows['b'] = original
        self.assertFalse(self.rotation.config()['enabled'])
        self.run.assert_not_called()

    def test_status_never_serializes_credentials_or_cache_fingerprints(self):
        self.manager.rows['a']['credential'] = {'refresh_token': 'secret-refresh'}
        self.manager.rows['a']['fingerprint'] = 'secret-fingerprint'
        self.manager.rows['a']['usage']['native_auth'] = 'secret-access'
        self.manager.rows['a']['usage']['windows'][0]['credential'] = 'secret-window'
        state = self.enable()
        raw = json.dumps(state) + (self.rotation.control/'status.json').read_text()
        self.assertNotIn('secret-', raw)
        self.assertEqual(state['profiles'][0]['usage']['windows'][0]['percent'], 25)

    def test_all_accounts_limited_wait_without_replacing_current_credentials(self):
        self.enable()
        for row in self.manager.rows.values():
            row['limited'] = True
        state = self.rotation.tick()
        self.assertEqual((state['active'], state['state']), ('a', 'waiting'))
        self.assertEqual(self.manager.activations, ['a'])

    def test_bootstrap_failure_disables_rotation(self):
        self.run.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]
        with self.assertRaisesRegex(RuntimeError, 'Could not start'):
            self.rotation.enable(['a', 'b'])
        self.assertFalse(self.rotation.config()['enabled'])
        self.assertFalse(self.rotation.agent_path().exists())


if __name__ == '__main__':
    unittest.main()
