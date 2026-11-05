import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from server.store import Store,Problem
from server.runner import Runner,execution_prompt
from server import conversation
from server.app import Application,Server
import test_core as core


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.req=conversation.new_request(self.store,{'message':'기획만 설명해줘','execution_authorized':False})
        self.p=self.store.create(self.req,'first')
    def tearDown(self):self.tmp.cleanup()
    def test_create_idempotency_keeps_one_exact_message(self):
        self.store.create(self.req,'first')
        rows=conversation.messages(self.store,self.p['id'])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['content'],'기획만 설명해줘')
        self.assertEqual(rows[0]['state'],'pending')
    def test_busy_message_waits_for_next_turn_and_preserves_original(self):
        j=self.store.claim();self.assertEqual(j['request']['agent_current_messages'][0]['message'],'기획만 설명해줘')
        conversation.post_message(self.store,j['id'],{'message':'이번엔 30초로 만들어줘\n$(touch NEVER_EXECUTE)','client_message_id':'next','execution_authorized':True})
        current=self.store.project(j['id']);self.assertEqual(current['status'],'running')
        self.assertEqual(current['request']['user_prompt'],'기획만 설명해줘')
        self.assertEqual(len(current['request']['agent_current_messages']),1)
        self.store.update(j['id'],status='awaiting_input');conversation.finish_turn(self.store,j['id'])
        nxt=self.store.claim();self.assertEqual(nxt['attempt'],2)
        self.assertEqual(nxt['request']['agent_current_messages'][0]['message'],'이번엔 30초로 만들어줘\n$(touch NEVER_EXECUTE)')
        self.assertTrue(nxt['request']['execution_authorized'])
    def test_duplicate_message_is_not_a_duplicate_turn(self):
        d={'message':'이 장면을 설명해줘','client_message_id':'same'}
        a=conversation.post_message(self.store,self.p['id'],d);b=conversation.post_message(self.store,self.p['id'],d)
        self.assertEqual(a['id'],b['id']);self.assertEqual(len(conversation.messages(self.store,self.p['id'])),2)
        with self.assertRaises(Problem):conversation.post_message(self.store,self.p['id'],{**d,'message':'다른 메시지'})
    def test_failure_does_not_blindly_submit_pending_command(self):
        j=self.store.claim();conversation.post_message(self.store,j['id'],{'message':'다음 요청','client_message_id':'next'})
        self.store.update(j['id'],status='failed');conversation.finish_turn(self.store,j['id'])
        self.assertIsNone(self.store.claim())
        rows=conversation.messages(self.store,j['id']);self.assertEqual([x['state'] for x in rows],['failed','pending'])
    def test_restart_recovers_processing_without_auto_execution(self):
        self.store.claim();self.store.recover();self.assertIsNone(self.store.claim())
        self.assertEqual(conversation.messages(self.store,self.p['id'])[0]['state'],'pending')
    def test_message_arriving_during_block_flush_wakes_only_after_cleanup(self):
        j=self.store.claim();self.store.update(j['id'],status='failed')
        conversation.post_message(self.store,j['id'],{'message':'검수 근거가 추가됐어. 이어서 확인해줘','client_message_id':'after-block'},defer_active=True)
        self.assertEqual(self.store.project(j['id'])['status'],'failed')
        conversation.finish_turn(self.store,j['id'])
        nxt=self.store.claim();self.assertEqual(nxt['request']['agent_current_messages'][0]['message'],'검수 근거가 추가됐어. 이어서 확인해줘')
        self.assertEqual(conversation.messages(self.store,j['id'])[0]['state'],'failed')
    def test_readonly_batch_cannot_inherit_an_earlier_generation_grant(self):
        conversation.post_message(self.store,self.p['id'],{'message':'作業前の説明だけ','client_message_id':'other','execution_authorized':True})
        j=self.store.claim();self.assertFalse(j['request']['execution_authorized'])
        r=Runner.__new__(Runner);r.store=self.store
        args,_=r.mcp_execution_args(j['request'])
        self.assertIn('features.image_generation=false',args);self.assertIn('sandbox_mode="read-only"',args)
        with self.assertRaises(Problem):r.action(j['id'],'tts',{'text':'forbidden'})
        with self.assertRaises(Problem):r.action(j['id'],'render',{'timeline':'x'})
    def test_configure_does_not_change_grants_limits_or_raw_message(self):
        j=self.store.claim();r=Runner.__new__(Runner);r.store=self.store
        root=self.store.project_root(j['id']);root.mkdir()
        result=r.action(j['id'],'configure',{'target_duration_s':30,'aspect_ratio':'16:9'})
        current=self.store.project(j['id'])
        self.assertEqual(result['target_duration_s'],30);self.assertEqual(current['request']['user_prompt'],self.req['user_prompt'])
        for payload in ({'target_duration_s':0},{'execution_authorized':True},{'limits':{}},{'connections':{}}):
            with self.assertRaises(Problem):r.action(j['id'],'configure',payload)
    def test_user_text_and_assistant_events_survive_reopen(self):
        self.store.event(self.p['id'],'agent','실제 Codex의 답변입니다.')
        reopened=Store(self.tmp.name);rows=conversation.messages(reopened,self.p['id'])
        self.assertEqual([x['role'] for x in rows],['user','assistant'])
        self.assertEqual(rows[-1]['content'],'실제 Codex의 답변입니다.')
    def test_agent_prompt_scopes_questions_without_forcing_generation(self):
        prompt=execution_prompt(Path(self.tmp.name),{'id':self.p['id'],'request':self.req})
        self.assertIn('execution_authorized=false',prompt);self.assertIn('AGENT CONVERSATION',prompt)
        self.assertIn('PRODUCTION_HANDOFF.md',prompt);self.assertNotIn("The user's recorded execution_authorized=true",prompt)
    def test_followup_is_passed_verbatim_to_cli_stdin_prompt(self):
        text='이 컷의 앵글만 바꿔줘.\n$(touch NEVER_EXECUTE) `literal`'
        req={**self.req,'agent_current_messages':[{'id':'msg_test','message':text}]}
        prompt=execution_prompt(Path(self.tmp.name),{'id':self.p['id'],'request':req})
        self.assertIn('MESSAGE msg_test\n'+text,prompt)
    def test_existing_production_can_join_chat_without_new_execution(self):
        request={**self.req};request.pop('interaction_mode')
        p=self.store.create(request,'legacy');self.store.update(p['id'],status='completed',session_id='original-session')
        a=conversation.attach_existing(self.store,p['id']);b=conversation.attach_existing(self.store,p['id'])
        self.assertEqual(a['session_id'],'original-session');self.assertEqual(b['status'],'completed')
        self.assertEqual(a['request']['user_prompt'],request['user_prompt'])
        rows=conversation.messages(self.store,p['id']);self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source'],'existing_project_request');self.assertEqual(rows[0]['state'],'done')
        with self.store.db() as db:pending=db.execute("SELECT COUNT(*) FROM chat_messages WHERE project_id=? AND state='pending'",(p['id'],)).fetchone()[0]
        self.assertEqual(pending,0)


class AgentHTTPTests(unittest.TestCase):
    call=core.HTTPTests.call
    tearDown=core.HTTPTests.tearDown
    def setUp(self):
        core.HTTPTests.setUp(self)
        r=Runner.__new__(Runner);r.store=self.app.store;r.auth_sessions={}
        r.status=lambda:{'codex_authenticated':True,'skills':True,'ffmpeg':False,'ffprobe':False,'missing_dependencies':[]}
        self.app.runner=r
    def test_readonly_chat_without_media_providers(self):
        status,_,body=self.call('/api/agent/projects','POST',{'message':'카메라 움직임을 기획만 설명해줘','execution_authorized':False},headers={'Idempotency-Key':'chat'})
        self.assertEqual(status,202,body)
        j=json.loads(body);self.assertEqual(j['request']['interaction_mode'],'agent')
        rows=json.loads(self.call('/api/projects/'+j['id']+'/messages')[2]);self.assertEqual(rows[0]['content'],'카메라 움직임을 기획만 설명해줘')
        code,_,raw=self.call('/api/projects/'+j['id']+'/messages','POST',{'message':'다음 명령','client_message_id':'next'})
        self.assertEqual(code,202,raw)
    def test_bad_message_and_private_reference_rejected(self):
        code,_,_=self.call('/api/agent/projects','POST',{'message':'','execution_authorized':False},headers={'Idempotency-Key':'chat'});self.assertEqual(code,400)
        code,_,_=self.call('/api/agent/projects','POST',{'message':'참고해','reference_urls':['http://127.0.0.1/private']},headers={'Idempotency-Key':'private'});self.assertEqual(code,400)


class AgentRunTests(unittest.TestCase):
    setUp=core.RunnerBoundaryTests.setUp
    tearDown=core.RunnerBoundaryTests.tearDown
    fake_codex=core.RunnerBoundaryTests.fake_codex
    def test_a_conversation_reply_does_not_require_a_nonexistent_video(self):
        with self.store.db() as db:
            req=self.job['request'];req['interaction_mode']='agent';req['execution_authorized']=False
            db.execute('UPDATE projects SET request=? WHERE id=?',(json.dumps(req),self.job['id']))
        self.job=self.store.project(self.job['id'])
        self.fake_codex('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"대화 답변만 제공합니다."}}))\n')
        with patch.object(self.runner,'env',return_value=self.env):self.runner.run(self.job)
        j=self.store.project(self.job['id']);self.assertEqual(j['status'],'awaiting_input');self.assertIsNone(j['result']);self.assertEqual(j['repair_count'],0)
