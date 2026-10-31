import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_rpc


FAKE_SERVER = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    with (home/'requests.jsonl').open('a') as out:
        out.write(json.dumps(request)+'\\n')
    if 'id' not in request: continue
    method = request['method']; params = request.get('params') or {}
    result = {}
    if method == 'initialize':
        result = {'userAgent':'fake'}
    elif method == 'account/read':
        result = {'account':{'type':'chatgpt','email':'test@example.test','planType':'plus'},'requiresOpenaiAuth':True}
    elif method == 'account/rateLimits/read':
        result = {'accountId':'workspace-test','rateLimits':{'primary':{'usedPercent':25,'windowDurationMins':300,'resetsAt':2000000000}}}
    elif method == 'account/login/start':
        if params['type'] == 'chatgpt':
            # Completion may precede the response to the login-start request.
            send({'method':'account/login/completed','params':{'loginId':'login-test','success':True}})
            result = {'type':'chatgpt','loginId':'login-test','authUrl':'https://auth.openai.com/test'}
        else:
            result = {'type':'chatgptDeviceCode','loginId':'device-test','verificationUrl':'https://auth.openai.com/device','userCode':'test-code'}
    elif method == 'account/login/cancel':
        send({'method':'account/login/completed','params':{'loginId':params['loginId'],'success':False,'error':'secret that must not leak'}})
        result = {'status':'canceled'}
    elif method == 'probe/environment':
        result = {'HOME':os.environ.get('HOME'),'CODEX_HOME':os.environ['CODEX_HOME'], 'cwd':os.getcwd(),'argv':sys.argv[1:], 'injected':any(k in os.environ for k in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','CODEX_SQLITE_HOME'))}
    elif method == 'probe/error':
        send({'id':request['id'],'error':params}); continue
    elif method == 'probe/timeout':
        continue
    elif method == 'probe/invalid':
        print('private invalid response body',flush=True); continue
    send({'id':request['id'],'result':result})
'''


class RpcTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='codex-rpc-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.home = self.root/'profile'
        self.server = self.root/'codex'
        self.server.write_text(FAKE_SERVER)
        self.server.chmod(0o755)

    def client(self, **kwargs):
        return codex_rpc.CodexRPC(self.home, executable=self.server, **kwargs)

    def requests(self):
        return [json.loads(line) for line in (self.home/'requests.jsonl').read_text().splitlines()]

    def test_real_stdio_handshake_account_usage_and_environment_isolation(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'secret','CODEX_API_KEY':'secret','CODEX_ACCESS_TOKEN':'secret','CODEX_SQLITE_HOME':'/wrong'}):
            with self.client(extra_config={'cli_auth_credentials_store':'file','sqlite_home':str(self.root/'shared db')}) as rpc:
                account = rpc.read_account(refresh=True)
                self.assertEqual(account['account']['email'], 'test@example.test')
                self.assertEqual(rpc.rate_limits()['rateLimits']['primary']['usedPercent'],25)
                env = rpc.call('probe/environment')
                self.assertFalse(env['injected'])
                self.assertEqual(env['HOME'], os.environ.get('HOME'))
                self.assertEqual(env['CODEX_HOME'], str(self.home))
                self.assertNotEqual(env['cwd'], str(self.home))
                self.assertIn('--no-daemon', env['argv'])
                self.assertIn('cli_auth_credentials_store="file"', env['argv'])
                self.assertIn('sqlite_home='+json.dumps(str(self.root/'shared db')), env['argv'])
                work = Path(env['cwd'])
            self.assertFalse(work.exists())
        requests = self.requests()
        self.assertEqual([r['method'] for r in requests[:2]], ['initialize','initialized'])
        quota = next(r for r in requests if r['method']=='account/rateLimits/read')
        self.assertEqual(quota['params'], {'excludeResetCreditDetails':True})

    def test_login_completion_can_arrive_before_start_response(self):
        with self.client() as rpc:
            login = rpc.login_start()
            self.assertEqual(rpc.wait_login(login['loginId'], timeout=1), {'loginId':'login-test','success':True})

    def test_device_login_timeout_cancels_server_attempt(self):
        with self.client() as rpc:
            login = rpc.login_start(device_code=True)
            with self.assertRaisesRegex(codex_rpc.RpcError, 'timed out'):
                rpc.wait_login(login['loginId'],timeout=.02)
        self.assertTrue(any(r['method']=='account/login/cancel' and r['params']=={'loginId':'device-test'} for r in self.requests()))

    def test_cancel_interrupts_waiter_without_exposing_remote_error(self):
        failures=[]
        with self.client() as rpc:
            login=rpc.login_start(device_code=True)
            def wait():
                try: rpc.wait_login(login['loginId'], timeout=2)
                except codex_rpc.RpcError as error: failures.append(error)
            thread=threading.Thread(target=wait)
            thread.start()
            rpc.cancel(login['loginId'])
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures[0].kind,'auth')
            self.assertNotIn('secret',str(failures[0]))

    def test_remote_errors_are_classified_without_returning_bodies(self):
        with self.client() as rpc:
            for message, kind in [('HTTP 429 too many requests','rate_limited'),
                                  ('HTTP 401 invalid_grant','auth'),
                                  ('quota exceeded','limited'), ('remote failed','unavailable')]:
                with self.subTest(kind=kind):
                    with self.assertRaises(codex_rpc.RpcError) as context:
                        rpc.call('probe/error',{'code':-32000,'message':message+' SECRET_BODY accessToken=private'})
                    self.assertEqual(context.exception.kind,kind)
                    self.assertNotIn('SECRET_BODY',str(context.exception))
                    self.assertNotIn('private',str(context.exception))
            with self.assertRaisesRegex(codex_rpc.RpcError,'does not support'):
                rpc.call('probe/error',{'code':-32601,'message':'private details'})

    def test_timeout_does_not_poison_next_request_and_close_is_idempotent(self):
        rpc=self.client()
        with rpc:
            with self.assertRaisesRegex(codex_rpc.RpcError,'timed out'):
                rpc.call('probe/timeout',timeout=.02)
            self.assertEqual(rpc.read_account()['account']['type'],'chatgpt')
        self.assertIsNotNone(rpc.process.poll())
        rpc.close()
        with self.assertRaises(codex_rpc.RpcError):rpc.call('account/read')

    def test_malformed_stdout_fails_without_exposing_it(self):
        with self.client() as rpc:
            with self.assertRaises(codex_rpc.RpcError) as context:
                rpc.call('probe/invalid')
            self.assertNotIn('private',str(context.exception))

    def test_missing_executable_is_sanitized(self):
        with self.assertRaisesRegex(codex_rpc.RpcError,'could not be started'):
            with codex_rpc.CodexRPC(self.home, executable=self.root/'missing-private-path'):
                pass


@unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
class NativeRpcTests(unittest.TestCase):
    def test_native_auth_save_preserves_canonical_symlinks(self):
        with tempfile.TemporaryDirectory(prefix='codex-native-auth-test-') as directory:
            root=Path(directory)
            vault, shared=root/'vault',root/'runtime'
            vault.mkdir();shared.mkdir()
            (vault/'auth.json').write_text(json.dumps({'OPENAI_API_KEY':'fake-old-key-no-inference'}))
            (vault/'auth.lock').touch()
            (shared/'auth.json').symlink_to(vault/'auth.json')
            (shared/'auth.lock').symlink_to(vault/'auth.lock')
            with codex_rpc.CodexRPC(shared,extra_config={'cli_auth_credentials_store':'file'}) as rpc:
                self.assertEqual(rpc.read_account()['account'],{'type':'apiKey'})
                result=rpc.call('account/login/start',{'type':'apiKey','apiKey':'fake-new-key-no-inference'})
                self.assertEqual(result,{'type':'apiKey'})
            self.assertTrue((shared/'auth.json').is_symlink())
            self.assertTrue((shared/'auth.lock').is_symlink())
            self.assertEqual(json.loads((vault/'auth.json').read_text())['OPENAI_API_KEY'],'fake-new-key-no-inference')

    def test_native_auth_is_cached_until_process_restart(self):
        with tempfile.TemporaryDirectory(prefix='codex-native-cache-test-') as directory:
            home=Path(directory)
            auth=home/'auth.json'
            auth.write_text(json.dumps({'OPENAI_API_KEY':'fake-key-no-inference'}))
            with codex_rpc.CodexRPC(home,extra_config={'cli_auth_credentials_store':'file'}) as rpc:
                self.assertEqual(rpc.read_account()['account'],{'type':'apiKey'})
                auth.unlink()
                self.assertEqual(rpc.read_account()['account'],{'type':'apiKey'})
            with codex_rpc.CodexRPC(home,extra_config={'cli_auth_credentials_store':'file'}) as rpc:
                self.assertIsNone(rpc.read_account()['account'])


if __name__ == '__main__':
    unittest.main()
