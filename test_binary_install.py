import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import binary_install as installer
import build_release
import runtime
import accounts_tui as tui
import codex_tui


class BinaryInstallTests(unittest.TestCase):
    def setUp(self):
        patch.object(installer.provider_app, 'choose', return_value='claude').start()
        self.options = patch.object(installer.setup_options, 'choose', return_value=['cli', 'desktop']).start()
        patch.object(installer.setup_options, 'load', return_value=['cli', 'desktop']).start()
        self.saved_options = patch.object(installer.setup_options, 'save').start()
        self.addCleanup(patch.stopall)

    def test_source_and_frozen_child_commands(self):
        script = Path('/example/desktop-sync.py')
        self.assertEqual(runtime.command(script, '--status-json'), [sys.executable, str(script), '--status-json'])
        with patch.object(sys, 'frozen', True, create=True):
            self.assertEqual(runtime.command(script, '--status-json'), [sys.executable, '--desktop-helper', '--status-json'])
            self.assertEqual(runtime.command(Path('/example/rotation.py'), '--tick'),
                             [sys.executable, '--rotation-helper', '--tick'])
            self.assertEqual(runtime.command(Path('/example/cli_import.py'), 'b', '/tmp/login file.json'),
                             [sys.executable, '--cli-import-helper', 'b', '/tmp/login file.json'])
            self.assertEqual(runtime.command(Path('/example/onboarding.py'), 'prepare', 'work'),
                             [sys.executable, '--onboarding-helper', 'prepare', 'work'])
            self.assertEqual(runtime.command(Path('/example/codex_accounts.py'), '--status-json'),
                             [sys.executable, '--codex-helper', '--status-json'])
            self.assertEqual(runtime.command(Path('/example/codex_rotation.py'), '--tick'),
                             [sys.executable, '--codex-rotation-helper', '--tick'])

    def test_install_missing_cli_and_skip_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(installer.subprocess, 'run') as run, patch.object(installer, 'download') as fetch:
                installer.install_cli(Path(directory), runner=run, fetch=fetch, which=lambda _: '/fake/claude')
                fetch.assert_not_called()
                run.assert_called_once_with(['/fake/claude', '--version'], check=True)
                run.reset_mock()
                with patch.object(installer.shutil, 'which', side_effect=[None, '/new/claude']) as which:
                    installer.install_cli(Path(directory), runner=run, fetch=fetch, which=which)
                fetch.assert_called_once_with(installer.CLI_INSTALLER, Path(directory)/'claude-install.sh')
                self.assertEqual(run.call_args_list[-1].args[0], ['/new/claude', '--version'])

    def test_download_failure_does_not_execute_installer(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer.subprocess, 'run') as run:
            with self.assertRaises(OSError):
                installer.install_cli(Path(directory), runner=run, which=lambda _: None,
                                      fetch=lambda *_: (_ for _ in ()).throw(OSError('offline')))
            run.assert_not_called()

    def test_desktop_download_checksum_signature_and_existing_app(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            target = work/'Applications/Claude.app'
            target.parent.mkdir()
            order = []
            checksum = 'a'*64
            def fetch(url, path):
                if url == installer.CASK:
                    path.write_text(json.dumps({'url':'https://downloads.claude.ai/example.zip','sha256':checksum}))
                else:
                    with zipfile.ZipFile(path,'w') as z:
                        z.writestr('Claude.app/Contents/Info.plist','fixture')
                return checksum
            def run(command, **kwargs):
                order.append(command)
                if '-x' in command:
                    (work/'desktop/Claude.app').mkdir(parents=True)
                if command[-1] == str(target):
                    target.mkdir()
            installer.install_desktop(work, target, run, fetch)
            self.assertEqual([cmd[0] for cmd in order], ['/usr/bin/ditto','/usr/bin/codesign','/usr/bin/ditto'])
            self.assertIn(installer.REQUIREMENT, order[1])
            with patch.object(installer, 'download') as untouched:
                installer.install_desktop(work, target, run, untouched)
                untouched.assert_not_called()

    def test_bad_checksum_blocks_extraction(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer.subprocess, 'run') as run:
            work = Path(directory)
            def fetch(url,path):
                path.write_text(json.dumps({'url':'https://downloads.claude.ai/example.zip','sha256':'a'*64}))
                return 'b'*64
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                installer.install_desktop(work, work/'Claude.app', run, fetch)
            run.assert_not_called()
        for url in ['http://downloads.claude.ai/example.zip','https://example.test/example.zip','https://downloads.claude.ai/example.dmg']:
            with self.assertRaises(RuntimeError):
                installer.desktop_metadata({'url':url,'sha256':'a'*64})

    def test_binary_install_replaces_launcher_preserving_account_data_and_quotes_spaces(self):
        with tempfile.TemporaryDirectory(prefix='account user ') as directory:
            home = Path(directory)
            source = home/'release'
            source.write_bytes(b'fake binary')
            credentials = home/'.claude-desktop-profiles'
            credentials.mkdir()
            (credentials/'private').write_text('unchanged')
            target = installer.install_binary(source,home)
            self.assertEqual(target.read_bytes(),b'fake binary')
            self.assertEqual(target.stat().st_mode & 0o777,0o755)
            self.assertEqual((credentials/'private').read_text(),'unchanged')
            helper = target.with_name('claude-desktop-sync')
            self.assertIn("'"+str(target)+"' --desktop-helper",helper.read_text())
            for name, arguments in (('codex-accounts', '--provider codex'),
                                    ('codex-rotate', '--codex-rotation-helper --run')):
                launcher = target.with_name(name)
                self.assertIn("'"+str(target)+"' "+arguments+' "$@"', launcher.read_text())
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
            installer.install_binary(target,home)
            target.unlink()
            target.symlink_to(source)
            with self.assertRaisesRegex(RuntimeError,'symlink'):
                installer.install_binary(source,home)
            self.assertEqual(source.read_bytes(),b'fake binary')

    def test_embedded_installer_payload_roundtrip_and_shell_syntax(self):
        binary = b'binary test content\x00\xff'
        script = build_release.installer_script(binary, 'arm64')
        self.assertIn(hashlib.sha256(binary).hexdigest(),script)
        payload=script.split("<<'CLAUDE_ACCOUNTS_BINARY'\n",1)[1].split('CLAUDE_ACCOUNTS_BINARY',1)[0]
        import base64
        self.assertEqual(base64.b64decode(payload),binary)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'install.command'
            path.write_text(script)
            subprocess.run(['/bin/bash','-n',str(path)],check=True)

    def test_component_selection_installs_only_requested_dependencies(self):
        for choice in ('cli', 'desktop', 'both'):
            with self.subTest(choice=choice), patch.object(sys,'frozen',True,create=True), \
                 patch.object(installer.os,'geteuid',return_value=501), \
                 patch.object(installer,'install_cli') as cli, patch.object(installer,'install_desktop') as desktop, \
                 patch.object(installer,'install_binary',return_value=Path('/example/claude-accounts')):
                self.assertEqual(installer.main(['--no-launch','--components='+choice]),0)
                self.assertEqual(cli.call_count,int(choice in ('cli','both')))
                self.assertEqual(desktop.call_count,int(choice in ('desktop','both')))
                self.saved_options.assert_called_with(['cli','desktop'] if choice=='both' else [choice])

    def test_cancel_selection_does_not_install_or_save_preferences(self):
        with patch.object(sys,'frozen',True,create=True), patch.object(installer.os,'geteuid',return_value=501), \
             patch.object(sys.stdin,'isatty',return_value=True),patch.object(sys.stdout,'isatty',return_value=True), \
             patch.object(installer,'install_cli') as cli, patch.object(installer,'install_desktop') as desktop, \
             patch.object(installer,'install_binary') as binary:
            self.options.return_value=None
            self.assertEqual(installer.main([]),0)
            for method in (cli,desktop,binary,self.saved_options):method.assert_not_called()

    def test_codex_and_both_install_only_selected_components(self):
        for provider in ('codex', 'both'):
            for choice in ('cli', 'desktop', 'both'):
                with self.subTest(provider=provider, components=choice), patch.object(sys, 'frozen', True, create=True), \
                     patch.object(installer.os, 'geteuid', return_value=501), \
                     patch.object(installer, 'install_cli') as claude_cli, \
                     patch.object(installer, 'install_desktop') as claude_desktop, \
                     patch.object(installer.codex_install, 'install_cli') as codex_cli, \
                     patch.object(installer.codex_install, 'install_desktop') as codex_desktop, \
                     patch.object(codex_tui, 'save_components') as save_codex, \
                     patch.object(installer, 'install_binary', return_value=Path('/example/claude-accounts')):
                    self.assertEqual(installer.main(['--no-launch', '--provider', provider, '--components', choice]), 0)
                    self.assertEqual(claude_cli.call_count, int(provider == 'both' and choice in ('cli', 'both')))
                    self.assertEqual(claude_desktop.call_count, int(provider == 'both' and choice in ('desktop', 'both')))
                    self.assertEqual(codex_cli.call_count, int(choice in ('cli', 'both')))
                    self.assertEqual(codex_desktop.call_count, int(choice in ('desktop', 'both')))
                    save_codex.assert_called_once_with(['cli', 'desktop'] if choice == 'both' else [choice])

    def test_unattended_existing_install_does_not_add_codex_dependencies(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(installer.os, 'geteuid', return_value=501), \
             patch.object(installer, 'install_cli'), patch.object(installer, 'install_desktop'), \
             patch.object(installer.codex_install, 'install_cli') as cli, \
             patch.object(installer.codex_install, 'install_desktop') as desktop, \
             patch.object(codex_tui, 'save_components') as save, \
             patch.object(installer, 'install_binary', return_value=Path('/example/claude-accounts')):
            self.assertEqual(installer.main(['--no-launch']), 0)
            for method in (cli, desktop, save):
                method.assert_not_called()

    def test_cancel_provider_or_codex_components_installs_nothing(self):
        for provider in (None, 'codex'):
            with self.subTest(provider=provider), patch.object(sys, 'frozen', True, create=True), \
                 patch.object(installer.os, 'geteuid', return_value=501), \
                 patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stdout, 'isatty', return_value=True), \
                 patch.object(installer.provider_app, 'choose', return_value=provider), \
                 patch.object(installer, 'install_cli') as cli, patch.object(installer, 'install_desktop') as desktop, \
                 patch.object(installer.codex_install, 'install_cli') as codex_cli, \
                 patch.object(installer.codex_install, 'install_desktop') as codex_desktop, \
                 patch.object(installer, 'install_binary') as binary, patch.object(codex_tui, 'save_components') as save:
                self.options.return_value = None
                self.assertEqual(installer.main([]), 0)
                for method in (cli, desktop, codex_cli, codex_desktop, binary, save):
                    method.assert_not_called()

    def test_failed_dependency_stops_before_app_install_and_tui(self):
        with patch.object(sys,'frozen',True,create=True), patch.object(installer.os,'geteuid',return_value=501), \
             patch.object(installer,'install_cli',side_effect=RuntimeError('offline')), \
             patch.object(installer,'install_desktop') as desktop, patch.object(installer,'install_binary') as binary, \
             patch.object(tui,'main') as ui:
            self.assertEqual(installer.main(['--no-launch']),1)
            desktop.assert_not_called()
            binary.assert_not_called()
            ui.assert_not_called()

    def test_success_launches_onboarding_after_install_and_no_launch_is_honored(self):
        with patch.object(sys,'frozen',True,create=True), patch.object(installer.os,'geteuid',return_value=501), \
             patch.object(sys.stdin,'isatty',return_value=True),patch.object(sys.stdout,'isatty',return_value=True), \
             patch.object(installer,'install_cli'),patch.object(installer,'install_desktop'), \
             patch.object(installer,'install_binary',return_value=Path('/example/claude-accounts')), \
             patch.object(tui,'main',return_value=0) as ui:
            self.assertEqual(installer.main([]),0)
            ui.assert_called_once_with(['--onboard'])
            ui.reset_mock()
            self.assertEqual(installer.main(['--no-launch']),0)
            ui.assert_not_called()


if __name__=='__main__':
    unittest.main()
