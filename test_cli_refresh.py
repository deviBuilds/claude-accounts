import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cli_refresh as cr


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.credential = {'accessToken': 'expired-secret', 'expiresAt': 1}
        self.identity_calls = []

    def read(self, config):
        self.assertEqual(config, self.home / '.claude-acct-b')
        return self.credential

    def identity(self, config):
        self.identity_calls.append(config)
        return 'b-id', 'b-org', 'b@example.test'

    def run_refresh(self, runner, **kwargs):
        return cr.refresh_account('b', self.home, self.read, self.identity, runner=runner, **kwargs)

    def test_renewed_credentials_win_over_model_failure(self):
        def runner(*_):
            self.credential = {'accessToken': 'new-secret', 'expiresAt': (time.time()+3600)*1000}
            return 'timeout'
        result = self.run_refresh(runner)
        self.assertEqual(result['cli_state'], 'ready')
        self.assertEqual(result['email'], 'b@example.test')
        self.assertNotIn('new-secret', json.dumps(result))
        self.assertNotIn('expired-secret', json.dumps(result))
        self.assertEqual(len(self.identity_calls), 1)

    def test_failure_cached_across_runs_and_manual_retry_allowed(self):
        calls = []
        def runner(*_):
            calls.append(1)
            return 'auth'
        self.assertEqual(self.run_refresh(runner)['cli_state'], 'sign_in_required')
        self.assertEqual(self.run_refresh(runner)['cli_state'], 'sign_in_required')
        self.assertEqual(len(calls), 1)
        self.run_refresh(runner, force=True)
        self.assertEqual(len(calls), 2)
        self.credential = {'accessToken': 'another-expired-secret', 'expiresAt': 2}
        self.run_refresh(runner)
        self.assertEqual(len(calls), 3)
        cache = self.home / '.claude-account-refresh/b.json'
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('secret', cache.read_text())
        self.assertFalse(self.identity_calls, 'Never send known-expired access tokens to profile API')

    def test_valid_credentials_skip_chargeable_request(self):
        self.credential['expiresAt'] = (time.time()+3600)*1000
        result = self.run_refresh(lambda *_: self.fail('Should not send a probe'))
        self.assertEqual(result['cli_state'], 'ready')

    def test_network_or_timeout_does_not_claim_login_revoked(self):
        result = self.run_refresh(lambda *_: 'timeout')
        self.assertEqual(result['cli_state'], 'refresh_failed')
        self.assertIn('timed out', result['cli_detail'])

    def test_crashed_attempt_is_not_repeated_automatically(self):
        def crash(*_):
            raise OSError('crash')
        with self.assertRaises(OSError):
            self.run_refresh(crash)
        result = self.run_refresh(lambda *_: self.fail('Repeated request'))
        self.assertEqual(result['cli_state'], 'refresh_failed')

    def test_environment_uses_only_selected_subscription(self):
        source = {'CLAUDE_CONFIG_DIR': '/wrong', 'ANTHROPIC_API_KEY': 'secret',
                  'ANTHROPIC_BASE_URL': 'https://example.test', 'CLAUDE_CODE_OAUTH_TOKEN': 'secret',
                  'CLAUDE_CODE_USE_BEDROCK': '1', 'CLAUDE_CODE_SIMPLE': '1', 'PATH': '/bin'}
        env = cr.environment('a', self.home, source)
        self.assertNotIn('CLAUDE_CONFIG_DIR', env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('ANTHROPIC_BASE_URL', env)
        self.assertNotIn('CLAUDE_CODE_SIMPLE', env)
        self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', env)
        self.assertEqual(cr.environment('b', self.home, source)['CLAUDE_CONFIG_DIR'], str(self.home / '.claude-acct-b'))
        self.assertIn('ANTHROPIC_API_KEY', source)

    def test_probe_timeout_terminates_process_and_disables_tools(self):
        with patch.object(cr.shutil, 'which', return_value='/fake/claude'), \
             patch.object(cr.subprocess, 'Popen') as popen, patch.object(cr, 'stop') as stop:
            process = popen.return_value
            process.poll.return_value = None
            self.assertEqual(cr.probe('b', self.home, threading.Event(), timeout=0), 'timeout')
            command = popen.call_args.args[0]
            self.assertIn('--safe-mode', command)
            self.assertIn('--no-session-persistence', command)
            self.assertEqual(command[command.index('--tools')+1], '')
            self.assertEqual(popen.call_args.kwargs['stdin'], cr.subprocess.DEVNULL)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            stop.assert_called_once_with(process)


if __name__ == '__main__':
    unittest.main()
