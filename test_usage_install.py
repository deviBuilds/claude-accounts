import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import account_usage as usage
import install
import onboarding
import desktop_profiles as dp
import account_status
import accounts_tui as tui


class UsageTests(unittest.TestCase):
    def test_current_and_legacy_schemas_percent_not_remaining(self):
        current = usage.normalize({'limits': [
            {'kind': 'session', 'percent': 37, 'resets_at': '2026-10-12T07:00:00Z'},
            {'kind': 'weekly_all', 'percent': 82, 'resets_at': None},
            {'kind': 'weekly_scoped', 'percent': 12, 'scope': {'model': {'display_name': 'Example model'}}}],
            'five_hour': {'utilization': 99}})
        self.assertEqual([w['percent'] for w in current], [37, 82, 12])
        self.assertEqual(current[2]['label'], 'Weekly — Example model')
        self.assertIsNone(current[1]['resets_at'])
        legacy = usage.normalize({'five_hour': {'utilization': 25, 'resets_at': '2026-10-12T07:00:00+00:00'},
                                  'seven_day': {'utilization': 0}, 'seven_day_sonnet': {'utilization': 50}})
        self.assertEqual([w['percent'] for w in legacy], [25, 0, 50])
        self.assertEqual(legacy[0]['resets_at'], current[0]['resets_at'])
        self.assertIn('37% used', usage.summary(current[0]))
        self.assertIn('82% used', usage.summary(current[1]))
        self.assertIn('unavailable', usage.summary(current[1]))

    def test_cache_rate_limit_and_stale_data_preserve_account_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            calls = []
            def fetch(token):
                calls.append(token)
                return [{'key': 'session', 'label': '5h', 'percent': 40, 'resets_at': 5000}]
            first = usage.load(home, 'a-id', {'accessToken': 'not-logged'}, now=1000, fetcher=fetch)
            usage.load(home, 'a-id', {'accessToken': 'not-logged'}, now=1100, fetcher=fetch)
            self.assertEqual(len(calls), 1)
            self.assertFalse(usage.load(home, 'b-id', now=1100)['windows'])
            def limited(_):
                raise urllib.error.HTTPError(usage.ENDPOINT, 429, 'limited', {'Retry-After': '0'}, None)
            stale = usage.load(home, 'a-id', {'accessToken': 'not-logged'}, now=1400, fetcher=limited)
            self.assertEqual(stale['state'], 'stale')
            self.assertEqual(stale['windows'], first['windows'])
            self.assertEqual(stale['checked_at'], 1000)
            self.assertEqual(stale['next_check'], 1460)
            cached = usage.load(home, 'a-id', {'accessToken': 'not-logged'}, now=1430, fetcher=lambda _: self.fail('429 backoff ignored'))
            self.assertEqual(cached['state'], 'stale')
            self.assertNotIn('not-logged', (home/'.claude-account-usage/a-id.json').read_text())
            self.assertEqual((home/'.claude-account-usage/a-id.json').stat().st_mode & 0o777, 0o600)

    def test_missing_reset_and_elapsed_reset_not_reported_as_zero_usage(self):
        self.assertIsNone(usage.reset_time('invalid'))
        self.assertEqual(usage.countdown(100, now=101), 'reset passed')
        self.assertEqual(usage.countdown(None), 'time unavailable')
        row = {'key': 'session', 'percent': 100, 'resets_at': 100}
        self.assertIn('100% used', usage.summary(row, now=101))
        self.assertIn('reset passed', usage.summary(row, now=101))


class FirstInstallTests(unittest.TestCase):
    def test_installs_under_a_different_home_without_private_state(self):
        with tempfile.TemporaryDirectory(prefix='different user ') as temp:
            home = Path(temp)
            launcher = install.install(Path(install.__file__).parent, home, Path(sys.executable))
            output = subprocess.run([str(launcher), '--help'], capture_output=True, text=True, check=True).stdout
            self.assertIn('--cli', output)
            self.assertFalse((home/'.zshrc').exists())
            self.assertFalse((home/'.claude-desktop-profiles').exists())
            self.assertEqual(sorted(p.name for p in (home/'.local/lib/claude-accounts').iterdir() if p.is_file()), sorted(install.FILES))
            self.assertIn(str(home/'.local/lib/claude-accounts/accounts_tui.py'), launcher.read_text())

    def test_cli_command_forwards_arguments_and_isolates_profile_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home/'.claude-acct-work').mkdir()
            with patch.object(Path, 'home', return_value=home), patch.object(tui.shutil, 'which', return_value='/bin/claude'), \
                 patch.object(tui.subprocess, 'call', return_value=0) as run:
                self.assertEqual(tui.main(['--cli', 'work', '--', '--continue']), 0)
                self.assertEqual(run.call_args.args[0], ['/bin/claude', '--continue'])
                self.assertEqual(run.call_args.kwargs['env']['CLAUDE_CONFIG_DIR'], str(home/'.claude-acct-work'))
                self.assertEqual(tui.main(['--cli', 'a', '--', '--version']), 0)
                self.assertNotIn('CLAUDE_CONFIG_DIR', run.call_args.kwargs['env'])

    def test_fresh_home_default_account_and_first_desktop_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            def missing(_):
                raise RuntimeError('No login yet')
            with patch.object(dp, 'desktop_account', return_value=None), patch.object(dp, 'desktop_state', return_value={'running': False, 'background': False}):
                state = account_status.collect(home, missing, missing)
            self.assertTrue(state['first_run'])
            self.assertEqual([a['label'] for a in state['accounts']], ['a'])
            manager = dp.Profiles(home, lambda _: ('account-id', 'org-id', 'new@example.test'), None)
            with manager.locked(), patch.object(dp, 'open_app') as launch:
                onboarding.prepare(manager, 'a')
                launch.assert_called_once()
            self.assertFalse(manager.root.exists())
            self.assertEqual(json.loads(onboarding.progress_path(manager, 'a').read_text())['account'], 'account-id')

    def test_new_profile_creates_shared_directories_in_empty_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            manager = dp.Profiles(home, None, None)
            with manager.locked():
                onboarding.create(manager, 'work')
            self.assertTrue((home/'.claude-acct-work/projects').is_dir())
            self.assertTrue((home/'.claude-acct-work/projects').is_symlink())
            self.assertEqual(json.loads((home/'.claude-acct-work/.claude.json').read_text()), {})


if __name__ == '__main__':
    unittest.main()
