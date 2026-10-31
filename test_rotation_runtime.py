"""Synthetic credentials only. Opt in with CLAUDE_ROTATION_SMOKE=1."""
import json,os,tempfile,subprocess,threading,time,http.server,queue
import unittest,shutil
from pathlib import Path
seen=[]
class Server(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{}')
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
  seen.append((self.path,self.headers.get('Authorization'),self.headers.get('x-api-key')))
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  msg={'id':'msg_test','type':'message','role':'assistant','model':'claude-sonnet-4-6','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':1}}
  events=[('message_start',{'type':'message_start','message':msg}),('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'OK'}}),('content_block_stop',{'type':'content_block_stop','index':0}),('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{'type':'message_stop'})]
  for event,payload in events:self.wfile.write(('event: '+event+'\ndata: '+json.dumps(payload)+'\n\n').encode())

@unittest.skipUnless(os.environ.get('CLAUDE_ROTATION_SMOKE') == '1' and shutil.which('claude'),
                     'Opt in to the installed CLI mock-server smoke test')
class PersistentCliTest(unittest.TestCase):
 def test_reload_between_requests(self):
  seen.clear()
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Server);threading.Thread(target=server.serve_forever,daemon=True).start()
  with tempfile.TemporaryDirectory(prefix='rotation-probe-') as tmp:
   root=Path(tmp)
   def creds(token):
    p=root/'.credentials.json';p.write_text(json.dumps({'claudeAiOauth':{'accessToken':token,'refreshToken':'fake','expiresAt':int((time.time()+3600)*1000),'scopes':['user:inference','user:profile'],'subscriptionType':'max','rateLimitTier':'default_claude_max_5x'}}));p.chmod(0o600)
   creds('sk-ant-oat01-probe-A')
   (root/'.claude.json').write_text(json.dumps({'hasCompletedOnboarding':True,'oauthAccount':{'accountUuid':'00000000-0000-4000-8000-000000000001','emailAddress':'probe@example.test','organizationUuid':'00000000-0000-4000-8000-000000000002'}}))
   env={k:v for k,v in os.environ.items() if not k.startswith(('ANTHROPIC_','CLAUDE_'))};env.update(CLAUDE_CONFIG_DIR=tmp,ANTHROPIC_BASE_URL='http://127.0.0.1:'+str(server.server_port),CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
   cmd=[shutil.which('claude'),'--safe-mode','-p','--input-format','stream-json','--output-format','stream-json','--verbose','--tools','','--no-session-persistence']
   p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,cwd=tmp,env=env)
   output=queue.Queue()
   def read():
    for line in p.stdout:
     try:output.put(json.loads(line))
     except ValueError:pass
   threading.Thread(target=read,daemon=True).start()
   def turn():
    p.stdin.write(json.dumps({'type':'user','message':{'role':'user','content':'Say OK.'}})+'\n');p.stdin.flush()
    deadline=time.time()+25
    while time.time()<deadline:
     try:r=output.get(timeout=1)
     except queue.Empty:
      if p.poll() is not None:raise AssertionError('CLI exited before completing a turn')
      continue
     if r.get('type')=='result':
      self.assertFalse(r.get('is_error'));return
    raise AssertionError('CLI turn timed out')
   try:
    turn();creds('sk-ant-oat01-probe-B');turn()
    self.assertEqual([auth for path,auth,_ in seen if path.startswith('/v1/messages')], ['Bearer sk-ant-oat01-probe-A','Bearer sk-ant-oat01-probe-B'])
   finally:
    p.terminate();p.wait(timeout=5)
    p.stdin.close();p.stdout.close()
    server.shutdown();server.server_close()

if __name__ == '__main__':
 unittest.main()
