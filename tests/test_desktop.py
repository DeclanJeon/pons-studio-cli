import json
import os
import queue
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_core as core
from server.codex_app_server import AppServerProcess
from server.desktop import DesktopEndpoint,environment,transport
from server.google import GoogleAuth
from server.store import Problem

FAKE='''import json,os,sys
scenario=os.environ.get("SCENARIO","")
for line in sys.stdin:
 m=json.loads(line)
 with open(os.environ["CAPTURE"],"a") as f:f.write(json.dumps(m)+"\\n")
 method=m.get("method")
 if method=="initialize":result={"userAgent":"test"}
 elif method in ("thread/start","thread/resume"):
  if scenario=="resume-fail":
   print(json.dumps({"id":m["id"],"error":{"message":"session missing"}}),flush=True);continue
  result={"thread":{"id":"other" if scenario=="wrong-session" else m["params"].get("threadId","same")}}
 elif method=="turn/start":result={"turn":{"id":"t1"}}
 elif method=="turn/interrupt":
  print(json.dumps({"id":m["id"],"result":{}}),flush=True)
  print(json.dumps({"method":"turn/completed","params":{"threadId":"same","turn":{"id":"t1","status":"interrupted"}}}),flush=True);continue
 else:continue
 print(json.dumps({"id":m["id"],"result":result}),flush=True)
 if method=="turn/start":
  if scenario=="disconnect":sys.exit(1)
  if scenario=="approval":print(json.dumps({"id":99,"method":"item/commandExecution/requestApproval","params":{}}),flush=True);continue
  if scenario=="wait":continue
  print(json.dumps({"method":"item/completed","params":{"threadId":"same","item":{"type":"agentMessage","text":"확인했습니다"}}}),flush=True)
  print(json.dumps({"method":"item/completed","params":{"threadId":"same","item":{"type":"mcpToolCall","server":"fish","tool":"get_voice","status":"completed"}}}),flush=True)
  print(json.dumps({"method":"turn/completed","params":{"threadId":"same","turn":{"id":"t1","status":"failed" if scenario=="failed" else "completed"}}}),flush=True)
'''

class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'fake.py').write_text(FAKE);self.processes=[]
    def tearDown(self):
        for p in self.processes:
            if p.poll() is None:
                try:os.killpg(p.pid,signal.SIGTERM)
                except ProcessLookupError:pass
            p.wait(timeout=8)
        self.temp.cleanup()
    def run_fake(self,scenario='',session=None,authorized=False):
        p=AppServerProcess([sys.executable,str(self.root/'fake.py')],{**os.environ,'CAPTURE':str(self.root/'capture'),'SCENARIO':scenario},str(self.root),'원문 명령\n첨부 자료',session,authorized)
        self.processes.append(p);return p
    def capture(self):return [json.loads(l) for l in (self.root/'capture').read_text().splitlines()]
    def test_new_thread_preserves_input_and_readonly_scope(self):
        p=self.run_fake();self.assertEqual(p.wait(8),0)
        calls=self.capture();self.assertEqual(calls[0]['method'],'initialize')
        start=next(c for c in calls if c.get('method')=='thread/start');self.assertEqual(start['params']['sandbox'],'read-only')
        turn=next(c for c in calls if c.get('method')=='turn/start');self.assertEqual(turn['params']['input'][0]['text'],'원문 명령\n첨부 자료')
        self.assertEqual(turn['params']['sandboxPolicy']['type'],'readOnly')
        values=[]
        while not p.lines.empty():
            label,line=p.lines.get()
            if label=='out':values.append(json.loads(line))
        self.assertTrue(any(v.get('item',{}).get('text')=='확인했습니다' for v in values))
        self.assertTrue(any(v.get('item',{}).get('type')=='mcp_tool_call' for v in values))
    def test_resume_never_creates_another_session(self):
        p=self.run_fake(session='same',authorized=True);self.assertEqual(p.wait(8),0)
        calls=self.capture();self.assertEqual(next(c for c in calls if c.get('method')=='thread/resume')['params']['threadId'],'same')
        self.assertFalse(any(c.get('method')=='thread/start' for c in calls))
        self.assertTrue(next(c for c in calls if c.get('method')=='turn/start')['params']['sandboxPolicy']['networkAccess'])
    def test_resume_failure_and_wrong_session_stop_before_turn(self):
        for scenario in ('resume-fail','wrong-session'):
            with self.subTest(scenario=scenario):
                (self.root/'capture').unlink(missing_ok=True)
                p=self.run_fake(scenario,session='same');self.assertEqual(p.wait(8),1)
                self.assertFalse(any(c.get('method') in ('thread/start','turn/start') for c in self.capture()))
    def test_failure_disconnect_and_unknown_approval_are_not_success(self):
        for scenario in ('failed','disconnect','approval'):
            with self.subTest(scenario=scenario):
                p=self.run_fake(scenario);self.assertEqual(p.wait(8),1)
        replies=[m for m in self.capture() if m.get('id')==99];self.assertEqual(replies[0]['error']['code'],-32601)
    def test_cancellation_closes_transport(self):
        p=self.run_fake('wait')
        import time
        deadline=time.monotonic()+3
        while p.turn_id is None and time.monotonic()<deadline:time.sleep(.02)
        os.killpg(p.pid,signal.SIGTERM);self.assertEqual(p.wait(8),1)
    def test_graceful_interrupt_uses_exact_thread_and_turn(self):
        p=self.run_fake('wait')
        import time
        deadline=time.monotonic()+3
        while p.turn_id is None and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(p.interrupt_and_wait())
        message=next(c for c in self.capture() if c.get('method')=='turn/interrupt')
        self.assertEqual(message['params'],{'threadId':'same','turnId':'t1'})

class DesktopHTTPTests(unittest.TestCase):
    setUp=core.HTTPTests.setUp
    tearDown=core.HTTPTests.tearDown
    call=core.HTTPTests.call
    def test_bootstrap_namespaces_selection_without_exposing_data_path_or_token(self):
        code,_,raw=self.call('/api/bootstrap',auth=False)
        self.assertEqual(code,200);info=json.loads(raw)
        self.assertEqual(info['workspace_id'],self.app.desktop.identity)
        self.assertEqual(info['workspace_id'],DesktopEndpoint(self.app).identity)
        self.assertNotIn(str(self.app.store.root),raw.decode());self.assertNotIn(self.app.desktop.token,raw.decode())
    def test_private_discovery_rejects_cookie_only_and_wrong_token(self):
        self.assertEqual(self.call('/api/desktop/health')[0],403)
        self.assertEqual(self.call('/api/desktop/health',headers={'X-Pons-Desktop':'wrong'})[0],403)
        code,_,raw=self.call('/api/desktop/health',headers={'X-Pons-Desktop':self.app.desktop.token},auth=False)
        self.assertEqual(code,200);self.assertFalse(json.loads(raw)['busy']);self.assertNotIn('token',raw.decode())
        self.app.store.create(core.request(),'busy');self.assertTrue(self.app.desktop.health()['busy'])
    def test_endpoint_record_private_and_old_owner_cannot_delete_new(self):
        endpoint=self.app.desktop;endpoint.publish()
        self.assertEqual(endpoint.path.stat().st_mode&0o777,0o600)
        replacement=DesktopEndpoint(self.app);replacement.publish();endpoint.close()
        self.assertTrue(replacement.path.exists());replacement.close();self.assertFalse(replacement.path.exists())
    def test_google_callback_uses_one_use_state_without_shared_cookie(self):
        auth=self.app.google
        info=auth.start('test.apps.googleusercontent.com','secret')
        import urllib.parse
        state=urllib.parse.parse_qs(urllib.parse.urlsplit(info['url']).query)['state'][0]
        with patch('server.google.call',return_value=(200,{},b'{"refresh_token":"opaque"}')):
            self.assertEqual(self.call('/api/oauth/google/callback?state='+state+'&code=test',auth=False)[0],200)
        self.assertEqual(self.call('/api/oauth/google/callback?state='+state+'&code=test',auth=False)[0],400)
        self.assertEqual(self.call('/api/oauth/google/callback?state=wrong&code=test',auth=False)[0],400)
    def test_transport_selection_has_no_implicit_paid_fallback(self):
        with patch.dict(os.environ,{'STUDIO_CODEX_TRANSPORT':'app-server'}):self.assertEqual(transport(self.app.store),'app-server')
    def test_shutdown_rejects_busy_and_blocks_new_jobs_after_idle_accept(self):
        self.app.store.create(core.request(),'busy')
        with self.assertRaises(Problem):self.app.desktop.begin_shutdown()
        self.assertFalse(self.app.desktop.closing)
        for job in self.app.store.projects():self.app.store.update(job['id'],status='completed')
        self.app.desktop.begin_shutdown()
        self.assertTrue(self.app.desktop.closing)
        with self.assertRaises(Problem):self.app.store.create(core.request(),'late')
    def test_skill_connection_requires_matching_supported_skill_and_idle(self):
        import types
        self.app.runner=types.SimpleNamespace(active={})
        path=Path(self.tmp.name)/'SKILL.md';path.write_text('---\nname: codex-imagen\n---\n')
        code,_,_=self.call('/api/environment/skill','POST',{'name':'codex-imagen','path':str(path)})
        self.assertEqual(code,200)
        saved=json.loads((self.app.store.root/'runtime.json').read_text());self.assertEqual(saved['additional_skill_paths'],[str(path)])
        self.assertEqual(self.call('/api/environment/skill','POST',{'name':'ai-delegation-loop','path':str(path)})[0],400)
        self.assertEqual(self.call('/api/environment/skill','POST',{'name':'unknown','path':str(path)})[0],400)
        self.app.store.create(core.request(),'busy')
        self.assertEqual(self.call('/api/environment/skill','POST',{'name':'codex-imagen','path':str(path)})[0],409)

if __name__=='__main__':unittest.main()
