import hashlib
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import time
import unittest


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='curl installer ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root/'bin'
        self.bin.mkdir()
        self.script = Path(__file__).with_name('install.sh')
        self.asset = 'claude-accounts-macos-arm64'
        binary = b'#!/bin/bash\nif [[ -t 0 && -t 1 ]]; then echo TTY_OK; fi\nprintf "%s\\n" "$@" > "$CA_TEST_DATA/executed"\n'
        (self.root/self.asset).write_bytes(binary)
        (self.root/(self.asset+'.sha256')).write_text(hashlib.sha256(binary).hexdigest()+'  '+self.asset+'\n')
        self.command('uname', 'if [[ "$1" == -s ]]; then echo Darwin; else echo "${CA_TEST_ARCH:-arm64}"; fi')
        self.command('sw_vers', 'echo 14.0')
        self.command('curl', '''[[ "${CA_TEST_OFFLINE:-0}" == 1 ]] && exit 22
url=''; out=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    https://*) url="$1" ;;
    -o) shift; out="$1" ;;
  esac
  shift
done
/bin/cp "$CA_TEST_DATA/${url##*/}" "$out"
''')
        self.env = {**os.environ, 'PATH':str(self.bin)+':'+os.environ['PATH'],
                    'CA_TEST_DATA':str(self.root), 'CA_TEST_SCRIPT':str(self.script)}

    def command(self, name, body):
        path = self.bin/name
        path.write_text('#!/bin/bash\n'+body+'\n')
        path.chmod(0o755)

    def run_installer(self, **env):
        return subprocess.run(['/bin/bash',str(self.script),'--check'], input='', capture_output=True,
                              text=True, env={**self.env,**env}, timeout=10)

    def test_verified_release_runs_check_without_a_terminal(self):
        result=self.run_installer()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'executed').read_text(),'--install\n--check\n')

    def test_tampering_never_executes_download(self):
        (self.root/self.asset).write_text('modified')
        result=self.run_installer()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('checksum mismatch',result.stderr)
        self.assertFalse((self.root/'executed').exists())

    def test_failed_download_and_unsupported_cpu_stop_before_execution(self):
        for env in ({'CA_TEST_OFFLINE':'1'},{'CA_TEST_ARCH':'x86_64'}):
            result=self.run_installer(**env)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((self.root/'executed').exists())

    def test_piped_script_gives_installer_a_real_terminal(self):
        pid,master=pty.fork()
        if pid==0:
            os.execve('/bin/bash',['bash','-c','cat "$CA_TEST_SCRIPT" | bash'],self.env)
        output=b''
        status=None
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try: output+=os.read(master,65536)
                    except OSError: break
            _,status=os.waitpid(pid,0)
            self.assertEqual(os.waitstatus_to_exitcode(status),0,output)
            self.assertIn(b'TTY_OK',output)
            self.assertEqual((self.root/'executed').read_text(),'--install\n')
        finally:
            if status is None:
                os.kill(pid,signal.SIGKILL)
                os.waitpid(pid,0)
            os.close(master)


if __name__=='__main__':
    unittest.main()
