import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

import account_usage
import rotation
import desktop_profiles as dp


class Backend:
    def __init__(self):
        self.rows = {x: self.ready(x) for x in ('a', 'b')}
        self.labels = ['a', 'b']

    def ready(self, label):
        return {'state': 'ready', 'account': label, 'organization': 'org', 'email': label+'@example.test',
                'credential': {'accessToken': 'fake-'+label, 'refreshToken': 'DO-NOT-COPY',
                               'expiresAt': int((time.time()+3600)*1000), 'scopes': ['user:inference']},
                'usage': {'state': 'ready', 'windows': [{'percent': 10, 'key': 'session'}]}}

    def available(self):
        return self.labels

    def identity(self, config):
        label = 'a' if config is None else config.name.removeprefix('.claude-acct-')
        r = self.rows[label]
        return r['account'], r['organization'], r['email']

    def inspect(self, label, cache):
        return self.rows[label]


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.backend = Backend()
        self.rotation = rotation.Rotation(self.home, self.backend)
        self.security = patch('rotation.subprocess.run', return_value=Mock(returncode=44))
        self.security.start()
        self.addCleanup(self.security.stop)

    def enable(self):
        self.rotation.enroll(['a', 'b'])

    def test_enrollment_requires_distinct_verified_accounts(self):
        self.backend.rows['b']['account'] = 'a'
        with self.assertRaises(RuntimeError):
            self.enable()
        self.assertFalse((self.rotation.control/'config.json').exists())

    def test_foreign_directory_is_not_overwritten(self):
        self.rotation.shared.mkdir()
        keep = self.rotation.shared/'keep'
        keep.write_text('unchanged')
        with self.assertRaises(RuntimeError):
            self.enable()
        self.assertEqual(keep.read_text(), 'unchanged')

    def test_keychain_shadow_is_refused(self):
        with patch('rotation.subprocess.run', return_value=Mock(returncode=0)):
            with self.assertRaisesRegex(RuntimeError, 'Keychain'):
                self.enable()

    def test_symlink_refused(self):
        self.rotation.control.symlink_to(self.home)
        with self.assertRaises(RuntimeError):
            self.enable()

    def test_access_only_private_credentials_and_status(self):
        self.enable()
        status = self.rotation.tick()
        token = self.rotation.shared/'.credentials.json'
        self.assertEqual(status['active'], 'a')
        self.assertNotIn('refreshToken', token.read_text())
        self.assertNotIn('fake-a', json.dumps(status))
        self.assertNotIn('DO-NOT-COPY', json.dumps(status))
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)

    def test_exhausted_account_rotates_without_touching_history(self):
        self.enable(); self.rotation.tick()
        history = self.rotation.shared/'projects'/'session.jsonl'
        history.parent.mkdir();history.write_text('keep conversation')
        self.backend.rows['a']['state'] = 'limited'
        status = self.rotation.tick()
        self.assertEqual(status['active'], 'b')
        self.assertEqual(history.read_text(), 'keep conversation')
        self.assertEqual(json.loads((self.rotation.shared/'.credentials.json').read_text())['claudeAiOauth']['accessToken'], 'fake-b')

    def test_stays_on_healthy_account(self):
        self.enable();self.rotation.tick()
        self.backend.rows['a']['state'] = 'limited';self.rotation.tick()
        self.backend.rows['a']['state'] = 'ready'
        self.assertEqual(self.rotation.tick()['active'], 'b')

    def test_failed_renewal_rotates(self):
        self.enable();self.rotation.tick()
        self.backend.rows['a'] = {'state': 'auth'}
        self.assertEqual(self.rotation.tick()['active'], 'b')

    def test_unknown_keeps_current_without_overwriting(self):
        self.enable();self.rotation.tick()
        before = (self.rotation.shared/'.credentials.json').read_bytes()
        self.backend.rows['a'] = {'state': 'unknown'}
        self.assertEqual(self.rotation.tick()['active'], 'a')
        self.assertEqual((self.rotation.shared/'.credentials.json').read_bytes(), before)

    def test_identity_drift_is_never_published(self):
        self.enable();self.rotation.tick()
        self.backend.rows['a']['account'] = 'different'
        status = self.rotation.tick()
        self.assertEqual(status['active'], 'b')
        self.assertEqual(status['profiles'][0]['state'], 'changed')

    def test_removed_account_is_not_used(self):
        self.enable();self.rotation.tick()
        self.backend.labels = ['b']
        self.assertEqual(self.rotation.tick()['active'], 'b')

    def test_every_account_exhausted_waits(self):
        self.enable();self.rotation.tick()
        for row in self.backend.rows.values():
            row['state'] = 'limited'
        status = self.rotation.tick()
        self.assertEqual(status['active'], 'a')
        self.assertEqual(status['state'], 'waiting')
        with self.assertRaises(RuntimeError):
            self.rotation.launch(['-p', 'hi'])

    def test_config_preserved_and_refreshed_token_republished(self):
        self.enable();self.rotation.tick()
        path = self.rotation.shared/'.claude.json'
        config=json.loads(path.read_text());config['mcpServers']={'example': {}}
        dp.atomic_json(path,config)
        self.backend.rows['a']['credential']['accessToken'] = 'fresh'
        self.rotation.tick()
        self.assertEqual(json.loads(path.read_text())['mcpServers'], {'example': {}})
        self.assertIn('fresh', (self.rotation.shared/'.credentials.json').read_text())

    def test_disable_retains_history_and_stops_polling(self):
        self.enable();self.rotation.tick()
        before=(self.rotation.shared/'.credentials.json').read_bytes()
        self.rotation.disable()
        self.assertFalse(self.rotation.tick()['enabled'])
        self.assertEqual(before,(self.rotation.shared/'.credentials.json').read_bytes())

    def test_agent_schedule_and_rollback(self):
        with patch('rotation.subprocess.run', side_effect=[Mock(returncode=44),Mock(returncode=0),Mock(returncode=0)]):
            self.rotation.enable(['a','b'])
        spec=rotation.plistlib.loads(self.rotation.agent_path().read_bytes())
        self.assertEqual(spec['StartInterval'],60)
        self.assertTrue(spec['RunAtLoad'])
        self.assertEqual(spec['ProgramArguments'][-1],'--tick')
        with patch('rotation.subprocess.run', side_effect=[Mock(returncode=44),Mock(returncode=0),Mock(returncode=1)]):
            with self.assertRaises(RuntimeError):
                self.rotation.enable(['a','b'])
        self.assertFalse(self.rotation.config()['enabled'])

    def test_launch_clears_auth_overrides_and_keeps_config_dir(self):
        self.enable();self.rotation.tick()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'wrong','CLAUDE_CODE_OAUTH_TOKEN':'wrong'}), patch('rotation.shutil.which',return_value='/usr/bin/claude'), patch('rotation.os.execve') as execute:
            self.rotation.launch(['-p','hello'])
        args=execute.call_args.args
        self.assertEqual(args[1], ['/usr/bin/claude','-p','hello'])
        self.assertEqual(args[2]['CLAUDE_CONFIG_DIR'],str(self.rotation.shared))
        self.assertNotIn('ANTHROPIC_API_KEY',args[2])
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN',args[2])

    def test_no_signin_into_shared_slot(self):
        with self.assertRaisesRegex(RuntimeError,'ordinary'):
            self.rotation.launch(['auth','login'])

    def test_expired_token_refresh_failure_is_rotatable(self):
        backend = rotation.Backend.__new__(rotation.Backend)
        backend.home = self.home
        backend.read_credentials = Mock(return_value={'accessToken': 'fake', 'expiresAt': 0})
        backend.identity = Mock()
        with patch('rotation.cli_refresh.refresh_account', side_effect=RuntimeError('Cannot read login')):
            self.assertEqual(backend.inspect('a', self.home)['state'], 'auth')

    def test_corrupt_status_does_not_crash_tui(self):
        self.rotation.control.mkdir()
        (self.rotation.control/'status.json').write_text('{')
        self.assertFalse(self.rotation.status()['enabled'])

    def test_one_minute_cache_and_backoff(self):
        fetch=Mock(return_value=[{'percent':10,'key':'session'}])
        for now in (100,120,161):
            account_usage.load(self.home,'test',{'accessToken':'fake'},now=now,fetcher=fetch,ttl=60)
        self.assertEqual(fetch.call_count,2)
        from urllib.error import HTTPError
        fetch.side_effect=HTTPError('https://example.test',429,'limited',{'Retry-After':'600'},None)
        account_usage.load(self.home,'test',{'accessToken':'fake'},now=222,fetcher=fetch,ttl=60)
        account_usage.load(self.home,'test',{'accessToken':'fake'},now=300,fetcher=fetch,ttl=60)
        self.assertEqual(fetch.call_count,3)


if __name__ == '__main__':
    unittest.main()
