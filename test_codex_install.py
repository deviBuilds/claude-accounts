import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_install as installer


def bundle(path, identity=installer.BUNDLE_ID):
    (path/'Contents').mkdir(parents=True)
    (path/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identity}))
    return path


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


class CodexInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='codex installer test ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.work = self.root/'work'
        self.work.mkdir()
        self.home = self.root/'home'
        self.home.mkdir()

    def test_desktop_detection_uses_bundle_identity_not_brand_name(self):
        apps = self.root/'Applications'
        apps.mkdir()
        bundle(apps/'Codex.app', 'not.openai.codex')
        bundle(apps/'ChatGPT.app', 'com.openai.chat')
        self.assertIsNone(installer.detect_desktop(roots=[apps]))
        current = bundle(apps/'Renamed Desktop.app')
        self.assertEqual(installer.detect_desktop(roots=[apps]), current)
        shutil.rmtree(apps/'ChatGPT.app')
        preferred = bundle(apps/'ChatGPT.app')
        self.assertEqual(installer.detect_desktop(roots=[apps]), preferred)

    def test_cli_detection_reuses_path_home_and_bundled_cli(self):
        visible = executable(self.root/'bin/codex')
        native = executable(self.home/'.local/bin/codex')
        self.assertEqual(installer.detect_cli(self.home, which=lambda _:str(visible)), visible)
        self.assertEqual(installer.detect_cli(self.home, which=lambda _:None), native)
        native.unlink()
        app = bundle(self.root/'ChatGPT.app')
        bundled = executable(app/'Contents/Resources/codex-cli')
        self.assertEqual(installer.detect_cli(self.home, which=lambda _:None, desktop=app), bundled)
        self.assertIsNone(installer.detect_cli(self.home, which=lambda _:None, include_bundled=False))

    def test_existing_cli_is_checked_and_never_reinstalled(self):
        native = executable(self.home/'.local/bin/codex')
        run, fetch = Mock(), Mock()
        self.assertEqual(installer.install_cli(self.work, self.home, run, fetch, lambda _:None), native)
        run.assert_called_once_with([str(native), '--version'], check=True)
        fetch.assert_not_called()
        run.side_effect = subprocess.CalledProcessError(1, 'codex')
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_cli(self.work, self.home, run, fetch, lambda _:None)
        fetch.assert_not_called()

    def test_native_packaged_cli_inside_desktop_is_detected(self):
        app = bundle(self.root/'Codex.app')
        native = executable(app/'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
        self.assertEqual(installer.bundled_cli(app), native)
        launcher = executable(app/'Contents/Resources/codex-cli/bin/codex')
        self.assertEqual(installer.bundled_cli(app), launcher)
        self.assertEqual(installer.detect_cli(self.home, which=lambda _:None, desktop=app), launcher)

    def test_native_install_verifies_signature_and_preserves_auth(self):
        auth = self.home/'.codex/auth.json'
        auth.parent.mkdir()
        auth.write_text('private sentinel')
        native = self.home/'.local/bin/codex'
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == '/bin/sh':
                executable(native)
                self.assertEqual(kwargs['env']['CODEX_NON_INTERACTIVE'], '1')
                self.assertEqual(kwargs['env']['CODEX_INSTALL_DIR'], str(native.parent))
                self.assertEqual(kwargs['env'].get('HOME'), os.environ.get('HOME'))
        fetch = Mock(side_effect=lambda _, path:path.write_text('#!/bin/sh\n'))
        self.assertEqual(installer.install_cli(self.work, self.home, run, fetch, lambda _:None), native)
        fetch.assert_called_once_with(installer.CLI_INSTALLER, self.work/'codex-install.sh')
        self.assertEqual(calls[1][0][0], '/usr/bin/codesign')
        self.assertIn(installer.TEAM_ID, calls[1][0][-2])
        self.assertEqual(calls[2][0], [str(native), '--version'])
        self.assertEqual(auth.read_text(), 'private sentinel')

    def test_download_failure_never_executes_installer(self):
        run = Mock()
        with self.assertRaises(OSError):
            installer.install_cli(self.work, self.home, run, Mock(side_effect=OSError('offline')), lambda _:None)
        run.assert_not_called()

    def test_missing_cli_output_and_conflicting_file_are_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'usable CLI'):
            installer.install_cli(self.work, self.home, Mock(), Mock(), lambda _:None)
        target = self.home/'.local/bin/codex'
        target.parent.mkdir(parents=True)
        target.symlink_to('/not/a/real/executable')
        fetch = Mock()
        with self.assertRaisesRegex(RuntimeError, 'conflicting'):
            installer.install_cli(self.work, self.home, Mock(), fetch, lambda _:None)
        fetch.assert_not_called()

    def test_existing_desktop_is_reused_without_opening_it(self):
        app = bundle(self.root/'ChatGPT.app')
        run, fetch = Mock(), Mock()
        with patch.object(installer, 'detect_desktop', return_value=app):
            self.assertEqual(installer.install_desktop(self.work, home=self.home, runner=run, fetch=fetch), app)
        run.assert_not_called()
        fetch.assert_not_called()

    def desktop_runner(self, identity=installer.BUNDLE_ID, signature_ok=True):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[:2] == ['/usr/bin/hdiutil', 'attach']:
                bundle(self.work/'codex-mount/ChatGPT.app', identity)
            elif command[0] == '/usr/bin/codesign' and not signature_ok:
                raise subprocess.CalledProcessError(1, command)
            elif command[0] == '/usr/bin/ditto':
                shutil.copytree(command[1], command[2])
        return run, calls

    def test_desktop_download_signature_check_copy_and_detach(self):
        target = self.root/'Codex.app'
        run, calls = self.desktop_runner()
        fetch = Mock(side_effect=lambda _, path:path.write_bytes(b'image'))
        with patch.object(installer, 'detect_desktop', return_value=None):
            self.assertEqual(installer.install_desktop(self.work, target, self.home, run, fetch), target)
        fetch.assert_called_once_with(installer.DESKTOP_DOWNLOAD, self.work/'Codex.dmg')
        self.assertEqual([c[0] for c in calls], ['/usr/bin/hdiutil', '/usr/bin/codesign', '/usr/bin/ditto', '/usr/bin/hdiutil'])
        self.assertEqual(calls[1][-2], installer.REQUIREMENT)
        self.assertIn('-readonly', calls[0])
        self.assertEqual(calls[-1][1], 'detach')
        self.assertEqual(installer.bundle_identity(target), installer.BUNDLE_ID)

    def test_invalid_signature_does_not_copy_and_always_detaches(self):
        target = self.root/'Codex.app'
        run, calls = self.desktop_runner(signature_ok=False)
        with patch.object(installer, 'detect_desktop', return_value=None):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install_desktop(self.work, target, self.home, run, Mock())
        self.assertFalse(target.exists())
        self.assertNotIn('/usr/bin/ditto', [c[0] for c in calls])
        self.assertEqual(calls[-1][1], 'detach')

    def test_wrong_desktop_bundle_is_rejected(self):
        target = self.root/'Codex.app'
        run, calls = self.desktop_runner(identity='com.openai.chat')
        with patch.object(installer, 'detect_desktop', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'layout'):
                installer.install_desktop(self.work, target, self.home, run, Mock())
        self.assertFalse(target.exists())
        self.assertEqual(calls[-1][1], 'detach')


if __name__ == '__main__':
    unittest.main()
