import plistlib
from pathlib import Path
from types import SimpleNamespace
import signal
import tempfile
import unittest
from unittest.mock import patch

import codex_desktop as desktop


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-desktop-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.app = self.home/'Applications/ChatGPT.app'
        (self.app/'Contents/MacOS').mkdir(parents=True)
        self.main = self.app/'Contents/MacOS/ChatGPT'
        self.main.write_bytes(b'fixture')
        (self.app/'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'ChatGPT'}))
        self.runtime = self.home/'runtime'
        self.user_data = self.home/'desktop data'
        bundled = self.app/'Contents/Resources/codex-cli/bin/codex'
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b'fixture')
        bundled.chmod(0o755)
        self.helper = str(bundled)+' app-server'

    def test_renamed_desktop_is_discovered_by_identity(self):
        self.assertEqual(desktop.installed_app(self.home), self.app)
        (self.app/'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'com.openai.chat', 'CFBundleExecutable': 'ChatGPT'}))
        self.assertIsNone(desktop._app_info(self.app))

    def test_scoped_instance_excludes_other_desktop_and_external_cli(self):
        processes = {
            10: {'ppid': 1, 'command': str(self.main)},
            11: {'ppid': 10, 'command': self.helper},
            20: {'ppid': 1, 'command': str(self.main)+' --user-data-dir='+str(self.user_data)},
            21: {'ppid': 20, 'command': self.helper},
            30: {'ppid': 1, 'command': '/usr/local/bin/codex exec --json'},
        }
        with patch.object(desktop, '_processes', return_value=processes), \
             patch.object(desktop, '_uses_runtime', return_value=False):
            state = desktop.desktop_state(self.runtime, self.user_data, self.app)
        self.assertEqual(state['pids'], [20])
        self.assertEqual(state['helper_pids'], [21])
        self.assertTrue(state['managed'])
        self.assertTrue(state['busy'])
        self.assertFalse(state['unknown_background'])

    def test_runtime_file_descriptors_do_not_prove_gui_ownership(self):
        processes = {21: {'ppid': 1, 'command': self.helper}}
        with patch.object(desktop, '_processes', return_value=processes), \
             patch.object(desktop, '_uses_runtime', return_value=False):
            state = desktop.desktop_state(self.runtime, self.user_data, self.app)
        self.assertTrue(state['busy'])
        self.assertTrue(state['unknown_background'])
        self.assertEqual(state['helper_pids'], [])
        with patch.object(desktop, '_processes', return_value=processes), \
             patch.object(desktop, '_uses_runtime', return_value=True):
            state = desktop.desktop_state(self.runtime, self.user_data, self.app)
        self.assertEqual(state['helper_pids'], [])
        self.assertTrue(state['unknown_background'])

    def test_launch_sets_both_home_variables_and_electron_storage(self):
        with patch.object(desktop.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='codex-cli 0.166.0-alpha.5.1')) as run:
            desktop.launch_app(self.runtime, self.user_data, self.app)
        command = run.call_args.args[0]
        self.assertEqual(command, ['/usr/bin/open', '-n', '--env', 'CODEX_HOME='+str(self.runtime),
            '--env', 'CODEX_ELECTRON_USER_DATA_PATH='+str(self.user_data), str(self.app),
            '--args', '--user-data-dir='+str(self.user_data)])
        self.assertTrue(self.runtime.is_dir())
        self.assertTrue(self.user_data.is_dir())

    def test_old_bundled_core_is_rejected_before_runtime_creation(self):
        with patch.object(desktop.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='codex-cli 0.150.0')) as run:
            with self.assertRaisesRegex(RuntimeError, 'Update Codex Desktop'):
                desktop.launch_app(self.runtime, self.user_data, self.app)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.runtime.exists())
        self.assertFalse(self.user_data.exists())

    def test_quit_targets_pid_and_only_its_remaining_helper(self):
        before = {10: {'ppid': 1, 'command': str(self.main)},
                  11: {'ppid': 10, 'command': self.helper},
                  20: {'ppid': 1, 'command': str(self.main)+' --user-data-dir='+str(self.user_data)},
                  21: {'ppid': 20, 'command': self.helper},
                  30: {'ppid': 1, 'command': '/usr/local/bin/codex'}}
        after = {p:r for p,r in before.items() if p != 20}
        done = {p:r for p,r in after.items() if p != 21}
        with patch.object(desktop, '_processes', side_effect=[before, after, done]), \
             patch.object(desktop, '_uses_runtime', return_value=False), \
             patch.object(desktop.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(desktop.os, 'kill') as kill:
            desktop.quit_app(self.runtime, self.user_data, self.app)
        self.assertEqual(run.call_count, 1)
        script = run.call_args.args[0][-1]
        self.assertIn('WithProcessIdentifier(20)', script)
        self.assertNotIn('WithProcessIdentifier(10)', script)
        kill.assert_called_once_with(21, signal.SIGTERM)

    def test_refuses_to_kill_unattributed_app_server(self):
        processes = {99: {'ppid': 1, 'command': self.helper}}
        with patch.object(desktop, '_processes', return_value=processes), \
             patch.object(desktop, '_uses_runtime', return_value=True), \
             patch.object(desktop.os, 'kill') as kill, \
             patch.object(desktop.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'cannot be identified'):
                desktop.quit_app(self.runtime, self.user_data, self.app)
        kill.assert_not_called()
        run.assert_not_called()

    def test_failed_process_inventory_refuses_switch(self):
        with patch.object(desktop, '_processes', side_effect=OSError('denied')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot check'):
                desktop.desktop_state(self.runtime, self.user_data, self.app)

    def test_standalone_shared_app_server_blocks_without_terminating_anything(self):
        processes = {
            20: {'ppid': 1, 'command': str(self.main)+' --user-data-dir='+str(self.user_data)},
            50: {'ppid': 1, 'command': '/usr/local/bin/codex app-server'},
            51: {'ppid': 1, 'command': '/usr/local/bin/codex exec work'},
        }
        with patch.object(desktop, '_processes', return_value=processes), \
             patch.object(desktop, '_uses_runtime', side_effect=lambda pid, home: pid == 50), \
             patch.object(desktop.os, 'kill') as kill, \
             patch.object(desktop.subprocess, 'run') as run:
            state = desktop.desktop_state(self.runtime, self.user_data, self.app)
            self.assertEqual(state['external_runtime_pids'], [50])
            self.assertTrue(state['busy'])
            with self.assertRaisesRegex(RuntimeError, 'T3Code'):
                desktop.quit_app(self.runtime, self.user_data, self.app)
        kill.assert_not_called()
        run.assert_not_called()

    def test_standalone_busy_detection_also_works_without_desktop_installed(self):
        processes = {50: {'ppid': 1, 'command': '/usr/local/bin/codex app-server'}}
        with patch.object(desktop, 'installed_app', return_value=None), \
             patch.object(desktop, '_processes', return_value=processes), \
             patch.object(desktop, '_uses_runtime', return_value=True):
            state = desktop.desktop_state(self.runtime, self.user_data)
        self.assertFalse(state['installed'])
        self.assertTrue(state['busy'])
        self.assertEqual(state['external_runtime_pids'], [50])


if __name__ == '__main__':
    unittest.main()
