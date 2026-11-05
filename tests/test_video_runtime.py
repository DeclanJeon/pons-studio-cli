import tempfile
import unittest
from pathlib import Path
from server.store import Store
from server import conversation
from server.runner import execution_prompt


class VideoRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_agent_accepts_explicit_video_connection(self):
        req=conversation.new_request(self.store,{'message':'첨부 영상의 인물을 참고 인물로 교체해줘','execution_authorized':True,'options':{'motion_video_authorized':True,'connections':{'video':'conn_video'}}})
        self.assertEqual(req['connections']['video'],'conn_video')
    def test_selected_video_runtime_is_not_contradicted_by_release_ban(self):
        req=conversation.new_request(self.store,{'message':'첨부 영상의 인물을 참고 인물로 교체해줘','execution_authorized':True})
        req['connections']['video']='conn_video';req['motion_video_authorized']=True
        prompt=execution_prompt(Path(self.tmp.name),{'id':'prj_test','request':req})
        self.assertNotIn('Scope bans YouTube publishing and generated motion-video calls',prompt)
        self.assertIn('video_runtime',prompt)

from unittest.mock import patch
from server import video_runtime as video
from server.store import Problem

class VideoGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.conn=self.store.save_connection({'name':'Video','provider':'custom','transport':'cli','role':'video','config':{'command':['/installed/higgsfield'],'runtime':'higgsfield'}})
        self.store.set_connection_status(self.conn['id'],'verified')
        req=conversation.new_request(self.store,{'message':'replace character','execution_authorized':True,'options':{'motion_video_authorized':True,'connections':{'video':self.conn['id']}}})
        self.p=self.store.create(req,'first');self.job=self.store.claim();self.root=self.store.project_root(self.p['id']);self.root.mkdir();(self.root/'clip.mp4').write_bytes(b'actual source version');(self.root/'ref.png').write_bytes(b'actual reference version')
        self.schema={'type':'video','params':[{'name':'resolution','enum':['720p']},{'name':'prompt'},{'name':'image_references'},{'name':'video_references'}]}
        self.plan=[{'label':'0–8 seconds','model':'hf_mult_replace_object','params':{'resolution':'720p','prompt':'replace subject only','image_references':['ref.png'],'video_references':['clip.mp4']}}]
        self.calls=[]
        def provider(c,args,**kwargs):
            self.calls.append(args)
            if args[:2]==['model','get']:return self.schema
            if args[:2]==['generate','cost']:return {'credits':28}
            return {'id':'provider-job-1','status':'completed','results':{'rawUrl':'https://media.example/result.mp4'}}
        self.provider=provider;self.cli=patch.object(video,'cli',side_effect=provider);self.cli.start();self.probe=patch.object(video.media,'probe',return_value={'format':{'duration':'8'},'streams':[{'codec_type':'video'}]});self.probe.start()
    def tearDown(self):self.probe.stop();self.cli.stop();self.tmp.cleanup()
    def quote(self):return video.action(self.store,self.job,{'operation':'quote','items':self.plan})
    def approve(self,q):
        self.store.update(self.job['id'],status='awaiting_input');conversation.finish_turn(self.store,self.job['id']);video.approve(self.store,self.job['id'],q['quote_id']);self.job=self.store.claim()
    def test_quote_is_aggregate_and_never_submits(self):
        q=video.action(self.store,self.job,{'operation':'quote','items':self.plan*2})
        self.assertEqual(q['total_credits'],56);self.assertEqual(len(q['items']),2)
        self.assertFalse(any(a[:2]==['generate','create'] for a in self.calls))
        self.assertFalse((self.root/'video').exists())
    def test_no_video_submission_without_exact_quote_approval(self):
        q=self.quote()
        with self.assertRaises(Problem):video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0})
        self.assertFalse(any(a[:2]==['generate','create'] for a in self.calls))
    def test_cancelled_estimate_is_hidden_and_cannot_be_approved(self):
        q=self.quote();video.cancel_pending(self.store,self.job['id'],'user clarified still-image animatic')
        self.assertIsNone(video.read_quote(self.store,self.job['id']))
        self.store.update(self.job['id'],status='awaiting_input')
        with self.assertRaises(Problem):video.approve(self.store,self.job['id'],q['quote_id'])
        self.assertFalse(any(a[:2]==['generate','create'] for a in self.calls))
    def test_input_change_invalidates_approval(self):
        q=self.quote();(self.root/'ref.png').write_bytes(b'changed')
        self.store.update(self.job['id'],status='awaiting_input')
        with self.assertRaises(Problem):video.approve(self.store,self.job['id'],q['quote_id'])
    def test_approved_item_runs_once_and_uses_wait(self):
        q=self.quote();self.approve(q)
        a=video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0})
        b=video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0})
        self.assertEqual(a,b);creates=[a for a in self.calls if a[:2]==['generate','create']];self.assertEqual(len(creates),1);self.assertIn('--wait',creates[0])
    def test_uncertain_submission_is_never_automatically_repeated(self):
        q=self.quote();self.approve(q)
        def fail(c,args,**kwargs):
            if args[:2]==['generate','create']:raise Problem('transport timeout')
            return self.provider(c,args,**kwargs)
        with patch.object(video,'cli',side_effect=fail) as call:
            with self.assertRaises(Problem):video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0})
            again=video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0});self.assertEqual(again['state'],'submission_unknown')
            self.assertEqual(sum(a.args[1][:2]==['generate','create'] for a in call.call_args_list),1)
    def test_live_price_increase_prevents_submission(self):
        q=self.quote();self.approve(q)
        def changed(c,args,**kwargs):return {'credits':29} if args[:2]==['generate','cost'] else self.provider(c,args,**kwargs)
        with patch.object(video,'cli',side_effect=changed):
            with self.assertRaises(Problem):video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0})
        self.assertFalse(any(a[:2]==['generate','create'] for a in self.calls))
    def test_sixty_second_input_is_rejected_before_upload(self):
        with patch.object(video.media,'probe',return_value={'format':{'duration':'60'}}):
            with self.assertRaisesRegex(Problem,'4~30초'):self.quote()
        self.assertFalse(any(a[:2]==['generate','cost'] for a in self.calls))
    def test_nonvideo_models_and_path_escape_are_rejected(self):
        with patch.object(video,'cli',return_value={'type':'image'}):
            with self.assertRaises(Problem):self.quote()
        self.plan[0]['params']['video_references']=['../escape.mp4']
        with self.assertRaises(Problem):self.quote()
    def test_model_cannot_add_cli_control_flags(self):
        self.plan[0]['params']['wait']='false'
        with self.assertRaises(Problem):self.quote()
    def test_readonly_turn_cannot_request_quote_or_submit(self):
        r=object.__new__(__import__('server.runner',fromlist=['Runner']).Runner);r.store=self.store
        request={**self.job['request'],'execution_authorized':False}
        with self.store.db() as db:db.execute('update projects set request=? where id=?',( __import__('json').dumps(request),self.job['id']))
        for op in ('quote','submit'):
            with self.assertRaises(Problem):r.action(self.job['id'],'video',{'operation':op,'items':self.plan})
    def test_expired_quote_requires_new_approval(self):
        q=self.quote();saved=video.read_quote(self.store,self.job['id']);saved['expires_at']=0;video.atomic_json(video.quote_path(self.store,self.job['id'],q['quote_id']),saved)
        self.store.update(self.job['id'],status='awaiting_input')
        with self.assertRaises(Problem):video.approve(self.store,self.job['id'],q['quote_id'])

    def test_new_instruction_cannot_spend_old_scope_approval(self):
        q=self.quote();self.approve(q)
        self.job['request']['agent_current_messages']=[{'id':'different-command','message':'use a different person'}]
        with self.assertRaises(Problem):video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0})
        self.assertFalse(any(a[:2]==['generate','create'] for a in self.calls))
    def test_public_quote_does_not_expose_private_provider_payloads(self):
        q=self.quote();self.assertNotIn('input_hashes',q);self.assertNotIn('params',q['items'][0]);self.assertNotIn('connection_id',q)
    def test_pending_provider_response_is_not_a_completed_delivery(self):
        self.assertEqual(video.completed_job_ids({'id':'one','status':'pending','result_url':'https://media.example/x'}),set())
        self.assertEqual(video.completed_job_ids({'id':'one','status':'completed','result_url':'https://media.example/x'}),{'one'})

import test_agent as agent_tests
class VideoHTTPTests(unittest.TestCase):
    setUp=agent_tests.AgentHTTPTests.setUp
    tearDown=agent_tests.AgentHTTPTests.tearDown
    call=agent_tests.AgentHTTPTests.call
    def test_video_settings_preserve_original_and_do_not_queue(self):
        code,_,body=self.call('/api/agent/projects','POST',{'message':'original exact prompt','execution_authorized':False},headers={'Idempotency-Key':'video-http'})
        self.assertEqual(code,202);p=__import__('json').loads(body);self.app.store.update(p['id'],status='failed')
        c=self.app.store.save_connection({'name':'installed','provider':'custom','transport':'cli','role':'video','config':{'command':['/installed/higgsfield'],'runtime':'higgsfield'}});self.app.store.set_connection_status(c['id'],'ready')
        code,_,body=self.call('/api/projects/'+p['id']+'/video-settings','POST',{'connection_id':c['id'],'motion_video_authorized':True})
        saved=__import__('json').loads(body);self.assertEqual(code,200);self.assertEqual(saved['status'],'failed');self.assertEqual(saved['request']['user_prompt'],'original exact prompt');self.assertEqual(saved['request']['connections']['video'],c['id'])
    def test_video_settings_reject_wrong_role(self):
        _,_,body=self.call('/api/agent/projects','POST',{'message':'original'},headers={'Idempotency-Key':'video-http'})
        p=__import__('json').loads(body);self.app.store.update(p['id'],status='failed')
        c=self.app.store.save_connection({'name':'speech','provider':'typecast','transport':'api','role':'speech'});self.app.store.set_connection_status(c['id'],'verified')
        code,_,_=self.call('/api/projects/'+p['id']+'/video-settings','POST',{'connection_id':c['id'],'motion_video_authorized':True});self.assertEqual(code,400)
    def test_unknown_quote_cannot_be_forged_through_http(self):
        _,_,body=self.call('/api/agent/projects','POST',{'message':'original'},headers={'Idempotency-Key':'video-http'})
        p=__import__('json').loads(body)
        code,_,_=self.call('/api/projects/'+p['id']+'/video-approval','POST',{'quote_id':None});self.assertEqual(code,400)
        code,_,_=self.call('/api/projects/'+p['id']+'/video-approval','POST',{'quote_id':'vq_'+'a'*32});self.assertEqual(code,404)

class VideoDeliveryProfileTests(unittest.TestCase):
    setUp=VideoGatewayTests.setUp
    tearDown=VideoGatewayTests.tearDown
    quote=VideoGatewayTests.quote
    approve=VideoGatewayTests.approve
    def fixture(self):
        q=self.quote();self.approve(q);video.action(self.store,self.job,{'operation':'submit','quote_id':q['quote_id'],'index':0})
        root=self.root;(root/'10_DELIVERY').mkdir();(root/'09_QA').mkdir();(root/'.history/events').mkdir(parents=True);(root/'.history/events/fixture.md').write_text('Engineering test fixture, no real provider generation')
        movie=root/'10_DELIVERY/edit.mp4';movie.write_bytes(b'technical fixture video bytes')
        sha=video.digest(movie)
        result={'kind':'video_edit','video':'10_DELIVERY/edit.mp4','sha256':sha,'video_quote_id':q['quote_id'],'generation_results':[{'index':0,'provider_job_id':'provider-job-1','file':'10_DELIVERY/edit.mp4','sha256':sha}]}
        (root/'10_DELIVERY/render_result.json').write_text(__import__('json').dumps(result))
        (root/'project.json').write_text(__import__('json').dumps({'asset_registry':[{'id':'FINAL','path':'10_DELIVERY/edit.mp4','sha256':sha}],'delivery_spec':{'final_asset_id':'FINAL'}}))
        checks=[{'category':k,'status':'pass','evidence':'Engineering fixture evidence only'} for k in ('identity','visual','continuity','playback','timing')]+[{'category':k,'status':'not_applicable','evidence':'This silent fixture has no speech or captions'} for k in ('audio','captions')]
        qa={'video_sha256':sha,'checks':checks};(root/'09_QA/qa.json').write_text(__import__('json').dumps(qa))
        return result,qa
    def test_edit_does_not_require_invented_narration_or_captions(self):
        self.fixture()
        with patch.object(video.subprocess,'run',return_value=__import__('subprocess').CompletedProcess([],0)):
            out=video.validate_delivery(self.store,self.root,{'target_duration_s':8,'production_contract_version':1})
        self.assertEqual(out['technical_checks'],'pass');self.assertNotIn('captions',out)
    def test_unverified_identity_cannot_complete_a_video_edit(self):
        _,qa=self.fixture();qa['checks'][0]['status']='unverified';(self.root/'09_QA/qa.json').write_text(__import__('json').dumps(qa))
        with self.assertRaisesRegex(Problem,'identity'):video.validate_delivery(self.store,self.root,{'target_duration_s':8,'production_contract_version':1})
    def test_v2_edit_still_requires_storyboards_and_endpoint_handoff(self):
        self.fixture()
        with self.assertRaises(Problem):video.validate_delivery(self.store,self.root,{'target_duration_s':8,'production_contract_version':2})


class AnimaticDefaultTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_installed_video_runtime_is_never_auto_selected_for_animatic(self):
        c=self.store.save_connection({'name':'installed video','provider':'custom','transport':'cli','role':'video','config':{'command':['higgsfield'],'runtime':'higgsfield'}});self.store.set_connection_status(c['id'],'ready')
        request=conversation.new_request(self.store,{'message':'첨부 영상의 인물을 바꿔 재구성해줘','execution_authorized':True})
        self.assertFalse(request['connections'].get('video'))
        self.assertEqual(request['delivery_kind'],'animatic')
        self.assertFalse(request['motion_video_authorized'])
        prompt=execution_prompt(Path(self.tmp.name),{'id':'prj_test','request':request})
        self.assertIn('STILL-IMAGE ANIMATIC',prompt)
    def test_animatic_cannot_spend_video_credits_even_with_an_old_connection(self):
        c=self.store.save_connection({'name':'old video','provider':'custom','transport':'cli','role':'video','config':{'command':['higgsfield'],'runtime':'higgsfield'}})
        request={'connections':{'video':c['id']},'delivery_kind':'animatic','motion_video_authorized':False}
        with self.assertRaises(Problem):video.selected(self.store,request)

    def test_stale_video_connection_is_not_exposed_to_animatic_agent(self):
        from server.system_integration import connection_ids
        self.assertEqual(connection_ids({'connections':{'image':'codex-imagen','video':'stale'},'delivery_kind':'animatic'}),set())
