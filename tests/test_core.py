import hashlib
import http.client
import io
import json
import math
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from server.store import Store,Problem,confined,atomic_json
from server.runner import Runner,execution_prompt
from server.app import Application,Server
from server import media

def request():return {'user_prompt':'달을 처음 보는 로봇의 이야기\n$(touch NEVER_EXECUTE)','target_duration_s':60,'aspect_ratio':'16:9','inputs':[],'connections':{},'limits':{'wall_time_s':120,'max_tts_characters':1000,'max_tts_calls':2},'execution_authorized':True}
def wav_bytes(seconds=.5):
    out=io.BytesIO()
    with wave.open(out,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(48000)
        import struct
        f.writeframes(b''.join(struct.pack('<h',int(1000*math.sin(2*math.pi*440*i/48000))) for i in range(int(seconds*48000))))
    return out.getvalue()

class StoreTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_idempotency_preserves_exact_prompt(self):
        req=request();a=self.store.create(req,'same');b=self.store.create(req,'same')
        self.assertEqual(a['id'],b['id']);self.assertEqual(a['request']['user_prompt'],req['user_prompt']);self.assertEqual(len(self.store.projects()),1)
    def test_idempotency_conflict_rejected(self):
        self.store.create(request(),'same');req=request();req['target_duration_s']=180
        with self.assertRaises(Problem) as e:self.store.create(req,'same')
        self.assertEqual(e.exception.status,409)
    def test_queue_claim_once_under_race(self):
        self.store.create(request(),'a');results=[]
        ts=[threading.Thread(target=lambda:results.append(self.store.claim())) for _ in range(4)]
        [t.start() for t in ts];[t.join() for t in ts]
        self.assertEqual(sum(x is not None for x in results),1)
    def test_recover_does_not_resubmit_paid_work(self):
        p=self.store.create(request(),'a');self.store.claim();self.store.recover()
        self.assertEqual(self.store.project(p['id'])['status'],'interrupted');self.assertIsNone(self.store.claim())
    def test_recover_archive_preserves_retry_state(self):
        p=self.store.create(request(),'a');self.store.update(p['id'],status='archiving');self.store.recover()
        self.assertEqual(self.store.project(p['id'])['status'],'archive_failed');self.assertIsNone(self.store.claim())
    def test_secret_is_encrypted_and_not_returned(self):
        c=self.store.save_connection({'name':'voice','provider':'fish','role':'speech','transport':'api','api_key':'VERY_PRIVATE_12345'})
        self.assertNotIn('secret',c);self.assertNotIn('VERY_PRIVATE_12345',json.dumps(self.store.connections()))
        self.assertEqual(self.store.connection(c['id'],True)['secret'],'VERY_PRIVATE_12345')
        self.assertNotIn(b'VERY_PRIVATE_12345',(Path(self.tmp.name)/'studio.sqlite').read_bytes())
        self.assertEqual((Path(self.tmp.name)/'vault.key').stat().st_mode&0o777,0o600)
    def test_events_resume_by_sequence(self):
        p=self.store.create(request(),'a');first=self.store.events(p['id']);self.store.event(p['id'],'progress','음성 생성')
        rows=self.store.events(p['id'],first[-1]['seq']);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['message'],'음성 생성')
    def test_confined_blocks_traversal_and_symlink(self):
        root=Path(self.tmp.name)/'project';root.mkdir();outside=Path(self.tmp.name)/'private';outside.write_text('secret');(root/'link').symlink_to(outside)
        for p in ('../private','link',str(outside)):
            with self.assertRaises(Problem):confined(root,p)
    def test_atomic_json_not_partial(self):
        p=Path(self.tmp.name)/'nested'/'project.json';atomic_json(p,{'version':1});atomic_json(p,{'version':2})
        self.assertEqual(json.loads(p.read_text()),{'version':2});self.assertFalse(list(p.parent.glob('*.tmp')))
    def test_prompt_contains_original_and_bounded_scope(self):
        job={'id':'prj_123','request':request()};prompt=execution_prompt(Path(self.tmp.name),job)
        self.assertIn(request()['user_prompt'],prompt);self.assertIn('No YouTube publication',prompt)

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=Application(self.tmp.name,worker=False)
        self.server=Server(('127.0.0.1',0),self.app);self.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.cookie='studio_session='+self.app.cookie
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def call(self,path,method='GET',data=None,headers=None,auth=True):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        h={'Cookie':self.cookie} if auth else {}
        if method!='GET':h['X-Studio-Request']='1'
        h.update(headers or {})
        if isinstance(data,dict):data=json.dumps(data)
        c.request(method,path,body=data,headers=h);r=c.getresponse();body=r.read();result=(r.status,dict(r.getheaders()),body);c.close();return result
    def test_api_needs_session(self):self.assertEqual(self.call('/api/projects',auth=False)[0],401)
    def test_bootstrap_rejects_remote_origin(self):self.assertEqual(self.call('/api/bootstrap',headers={'Origin':'https://evil.example'},auth=False)[0],403)
    def test_mutations_require_csrf_header(self):self.assertEqual(self.call('/api/connections','POST',{},headers={'X-Studio-Request':'wrong'})[0],403)
    def test_dns_rebinding_host_rejected(self):self.assertEqual(self.call('/api/bootstrap',headers={'Host':'attacker.example'})[0],403)
    def test_upload_hash_and_path_sanitization(self):
        status,_,raw=self.call('/api/inputs','POST',b'hello reference',{'X-File-Name':'../../source.md'})
        self.assertEqual(status,201);obj=json.loads(raw);inp=self.app.store.input(obj['id'])
        self.assertEqual(inp['name'],'source.md');self.assertEqual(inp['sha256'],hashlib.sha256(b'hello reference').hexdigest())
    def test_unknown_upload_extension_rejected(self):self.assertEqual(self.call('/api/inputs','POST',b'bad',{'X-File-Name':'program.exe'})[0],400)
    def test_artifact_range_and_hidden_access(self):
        p=self.app.store.create(request(),'test');root=self.app.store.project_root(p['id']);root.mkdir();(root/'clip.mp4').write_bytes(b'0123456789');(root/'.studio-control.json').write_text('private')
        status,h,b=self.call(f'/api/projects/{p["id"]}/file?path=clip.mp4',headers={'Range':'bytes=2-5'})
        self.assertEqual(status,206);self.assertEqual(b,b'2345');self.assertEqual(h['Content-Range'],'bytes 2-5/10')
        self.assertEqual(self.call(f'/api/projects/{p["id"]}/file?path=.studio-control.json')[0],403)
        self.assertEqual(self.call(f'/api/projects/{p["id"]}/file?path=clip.mp4',headers={'Range':'bytes=20-'})[0],416)
    def test_private_store_is_not_a_static_route(self):
        status,_,body=self.call('/.studio/vault.key',auth=False)
        self.assertNotIn((Path(self.tmp.name)/'vault.key').read_bytes(),body)

    def test_progress_api_survives_reopen_and_reads_latest_beyond_event_page(self):
        p=self.app.store.create(request(),'progress')
        self.app.store.update(p['id'],status='failed',stage='characters',error='검수 도구 확인 필요')
        for i in range(260):self.app.store.event(p['id'],'progress',f'작업 {i}')
        self.app.store=Store(self.tmp.name)
        status,_,raw=self.call(f'/api/projects/{p["id"]}')
        view=json.loads(raw)
        self.assertEqual(status,200)
        self.assertEqual(view['progress']['latest_message'],'작업 259')
        self.assertEqual(view['progress']['percent'],25)
        self.assertEqual(view['error'],'검수 도구 확인 필요')
        self.assertFalse(view['progress']['clock_running'])
        listing=json.loads(self.call('/api/projects')[2])
        self.assertEqual(listing[0]['progress']['percent'],25)

class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.conn=self.store.save_connection({'name':'fish','provider':'fish','transport':'api','role':'speech','api_key':'TEST_SECRET'})
        req=request();req['connections']={'speech':self.conn['id']};self.p=self.store.create(req,'a');self.store.update(self.p['id'],status='running');self.store.project_root(self.p['id']).mkdir()
        self.runner=Runner.__new__(Runner);self.runner.store=self.store
        self.payload={'connection_id':self.conn['id'],'voice_id':'voice_01','text':'실제 테스트용 문장'}
    def tearDown(self):self.tmp.cleanup()
    def test_repeated_speech_is_cached_after_measured_file(self):
        with patch('server.providers.synthesize',return_value=(wav_bytes(),'.wav')) as submit:
            a=self.runner.action(self.p['id'],'tts',self.payload);b=self.runner.action(self.p['id'],'tts',self.payload)
        self.assertEqual(submit.call_count,1);self.assertEqual(a,b);self.assertAlmostEqual(a['duration_s'],.5,places=2)
    def test_lost_response_is_not_blindly_resubmitted(self):
        with patch('server.providers.synthesize',side_effect=TimeoutError()) as submit:
            with self.assertRaises(TimeoutError):self.runner.action(self.p['id'],'tts',self.payload)
            with self.assertRaises(Problem):self.runner.action(self.p['id'],'tts',self.payload)
        self.assertEqual(submit.call_count,1)
    def test_unselected_connection_rejected(self):
        with self.assertRaises(Problem) as e:self.runner.action(self.p['id'],'tts',{**self.payload,'connection_id':'conn_not_allowed'})
        self.assertEqual(e.exception.status,403)
    def test_call_budget_is_enforced(self):
        with patch('server.providers.synthesize',return_value=(wav_bytes(),'.wav')) as submit:
            self.runner.action(self.p['id'],'tts',self.payload)
            self.runner.action(self.p['id'],'tts',{**self.payload,'text':'두 번째'})
            with self.assertRaises(Problem):self.runner.action(self.p['id'],'tts',{**self.payload,'text':'세 번째'})
        self.assertEqual(submit.call_count,2)
    def test_confirmed_runtime_block_prevents_more_paid_submissions(self):
        result=self.runner.action(self.p['id'],'block',{'code':'auth','message':'선택한 이미지 공급자의 인증이 만료됐습니다.'})
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.store.project(self.p['id'])['status'],'failed')
        with patch('server.providers.synthesize') as submit:
            with self.assertRaises(Problem):self.runner.action(self.p['id'],'tts',self.payload)
            submit.assert_not_called()
        self.assertEqual(self.store.project(self.p['id'])['repair_count'],0)
    def test_typecast_catalog_keeps_real_names_and_model_versions(self):
        from server.providers import voices
        payload=[{'voice_id':'tc_public','voice_name':'Public narrator','voice_type':'original','models':[{'version':'ssfm-v31'}]}, {'voice_name':'missing ID'}]
        with patch('server.providers.request',return_value=(json.dumps(payload).encode(),'application/json')):
            result=voices({'provider':'typecast','transport':'api','secret':'TEST_SECRET'})
        self.assertEqual(result,[{'id':'tc_public','name':'Public narrator','voice_type':'original','models':[{'version':'ssfm-v31'}]}])

class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Image.new('RGB',(128,72),(60,80,140)).save(self.root/'image.png')
        (self.root/'speech.wav').write_bytes(wav_bytes(.5))
        self.timeline={'fps':24,'shots':[{'id':'shot1','start_frame':0,'end_frame':24,'image':'image.png'}],'audio':[{'id':'speech1','file':'speech.wav','start_s':0}],'captions':[{'start_s':0,'end_s':.5,'text':'기술 렌더 시험'}]}
    def tearDown(self):self.tmp.cleanup()
    def test_timeline_overlap_is_rejected(self):
        self.timeline['shots'].append({'id':'shot2','start_frame':23,'end_frame':48,'image':'image.png'})
        with self.assertRaises(Problem):media.validate_timeline(self.root,self.timeline)
    def test_audio_outside_video_is_rejected(self):
        self.timeline['audio'][0]['start_s']=.8
        with self.assertRaises(Problem):media.validate_timeline(self.root,self.timeline)
    def test_real_ffmpeg_animatic_decodes_with_measured_audio(self):
        result=media.render(self.root,self.timeline,size=(128,72),timeout=60)
        video=self.root/result['video'];self.assertTrue(video.is_file());self.assertAlmostEqual(result['duration_s'],1,delta=.15)
        self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(),result['sha256'])
        self.assertEqual(result['semantic_review'],'pending')
        streams=media.probe(video)['streams'];self.assertEqual({s['codec_type'] for s in streams},{'audio','video'})
    def test_cancel_render_stops_output(self):
        with self.assertRaises(Problem):media.render(self.root,self.timeline,cancelled=lambda:True,size=(128,72),timeout=30)

class RunnerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'data')
        self.bin=self.root/'bin';self.bin.mkdir()
        (self.store.root/'skills/source').mkdir()
        imagen=self.store.root/'skills/source/codex-imagen';imagen.mkdir()
        (imagen/'SKILL.md').write_text('---\nname: codex-imagen\n---\nFixture for runner lifecycle tests; never generates images.')
        self.runner=Runner.__new__(Runner);self.runner.store=self.store;self.runner.base_url='http://127.0.0.1:1'
        self.runner.guard=threading.RLock();self.runner.stop=threading.Event();self.runner.tokens={};self.runner.active={}
        self.env={**os.environ,'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],'CODEX_HOME':str(self.store.root/'codex')}
        self.store.create(request(),'job');self.job=self.store.claim()
    def tearDown(self):
        for p in self.runner.active.values():
            if p.poll() is None:self.runner.kill(p)
        self.tmp.cleanup()
    def fake_codex(self,code):
        import sys
        path=self.bin/'codex';path.write_text(f'#!{sys.executable}\n'+code);path.chmod(0o700)
    def test_exit_zero_cannot_claim_a_nonexistent_video(self):
        self.fake_codex('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"thread.started","thread_id":"exact_session_123"}))\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"작업을 완료했습니다"}}))\n')
        with patch.object(self.runner,'env',return_value=self.env):
            with self.assertRaises(Problem):self.runner.run(self.job)
        saved=self.store.project(self.job['id'])
        self.assertEqual(saved['session_id'],'exact_session_123');self.assertNotIn(saved['status'],['animatic_ready','completed'])
        self.assertTrue(any(e['kind']=='agent' for e in self.store.events(self.job['id'])))
    def test_cancellation_terminates_the_running_process(self):
        self.fake_codex('import sys,time\nsys.stdin.read()\ntime.sleep(30)\n')
        failures=[]
        def run():
            try:
                with patch.object(self.runner,'env',return_value=self.env):self.runner.run(self.job)
            except Exception as e:failures.append(e)
        thread=threading.Thread(target=run);thread.start()
        deadline=time.time()+5
        while not self.runner.active and time.time()<deadline:time.sleep(.02)
        self.assertTrue(self.runner.active);self.store.update(self.job['id'],status='cancelling')
        thread.join(5);self.assertFalse(thread.is_alive());self.assertEqual(failures,[])
        self.assertEqual(self.store.project(self.job['id'])['status'],'cancelled')
    def test_model_capacity_error_keeps_actual_cause(self):
        self.fake_codex('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"turn.failed","error":{"message":"Selected model is at capacity"}}))\nsys.exit(1)\n')
        with patch.object(self.runner,'env',return_value=self.env):
            with self.assertRaises(Problem) as error:self.runner.run(self.job)
        self.assertIn('Selected model is at capacity',error.exception.message)
        self.assertNotIn('인증과 공급자 연결',error.exception.message)
    def test_confirmed_block_allows_cli_to_flush_last_tool_result(self):
        marker=self.root/'flushed.marker'
        self.fake_codex('import sys,time\nfrom pathlib import Path\nsys.stdin.read()\ntime.sleep(.6)\nPath('+repr(str(marker))+').write_text("last tool response recorded")\n')
        failures=[]
        def run():
            try:
                with patch.object(self.runner,'env',return_value=self.env):self.runner.run(self.job)
            except Exception as e:failures.append(e)
        thread=threading.Thread(target=run);thread.start()
        deadline=time.time()+3
        while not self.runner.active and time.time()<deadline:time.sleep(.01)
        self.runner.action(self.job['id'],'block',{'code':'observation','message':'실제 청취 근거가 없습니다.'})
        thread.join(4)
        self.assertFalse(thread.is_alive());self.assertEqual(failures,[])
        self.assertTrue(marker.exists(),'The final tool result must flush before terminating the CLI')
        self.assertEqual(self.store.project(self.job['id'])['status'],'failed')
    def test_validation_repairs_stop_after_two_automatic_passes(self):
        self.fake_codex('import sys,json\nsys.stdin.read()\nprint(json.dumps({"type":"thread.started","thread_id":"fixture_session"}))\n')
        self.store.update(self.job['id'],status='queued',attempt=0,started_at=None)
        with patch.object(Runner,'env',return_value=self.env):
            worker=Runner(self.store,'http://127.0.0.1:1')
            try:
                deadline=time.time()+10
                while time.time()<deadline and self.store.project(self.job['id'])['status']!='failed':time.sleep(.05)
                job=self.store.project(self.job['id'])
                self.assertEqual(job['status'],'failed');self.assertEqual(job['repair_count'],2);self.assertEqual(job['attempt'],3)
            finally:worker.stop.set();worker.worker.join(5)

class DeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        Image.new('RGB',(128,72),(60,80,140)).save(self.root/'image.png');(self.root/'speech.wav').write_bytes(wav_bytes(.5))
        timeline={'fps':24,'shots':[{'id':'SH01','start_frame':0,'end_frame':24,'image':'image.png'}],'audio':[{'id':'SP01','file':'speech.wav','start_s':0}],'captions':[{'start_s':0,'end_s':.5,'text':'검증 전용'}]}
        self.result=media.render(self.root,timeline,size=(128,72),timeout=60)
        self.store=Store(Path(self.tmp.name)/'data')
        source=Path(__file__).resolve().parent.parent/'.studio/skills/source'
        if not source.is_dir() and os.environ.get('PONS_TEST_SKILLS_SOURCE'):source=Path(os.environ['PONS_TEST_SKILLS_SOURCE']).expanduser().resolve()
        if not source.is_dir():self.skipTest('외부 스킬 통합 시험: PONS_TEST_SKILLS_SOURCE를 고정 버전 소스로 지정하세요.')
        (self.store.root/'skills/source').symlink_to(source,target_is_directory=True)
        self.runner=Runner.__new__(Runner);self.runner.store=self.store
        self.req={**request(),'target_duration_s':1}
        p={'schema_version':'1.1','project_id':'fixture','version':'1','aspect_ratio':'16:9','target_duration_s':1,'fps':24,'audio_mode':'dialogue',
          'scenes':[{'id':'SC01','purpose':'technical fixture'}],'characters':[],
          'shots':[{'id':'SH01','scene_id':'SC01','character_ids':[],'start_s':0,'end_s':1,'source_duration_s':1,'source_duration_status':'measured','purpose':'technical fixture','start_state':'test image','end_state':'test image'}],
          'claims':[],'artifacts':[{'id':'A01','type':'plan','version':'1','status':'draft','dependencies':[],'dependency_versions':{}}],
          'asset_registry':[{'id':'AS_FINAL','kind':'animatic','version':'1','status':'verified','path':self.result['video'],'sha256':self.result['sha256']}],
          'audio_cues':[{'id':'AU01','start_s':0,'end_s':.5,'layer':'voiceover'}],
          'captions':[{'id':'CAP01','start_s':0,'end_s':.5,'text':'검증 전용'}],'issues':[],
          'delivery_spec':{'destination':'test','width':128,'height':72,'container':'mp4','video_codec':'h264','caption_mode':'burned_in','final_asset_id':'AS_FINAL'},
          'delivery_checks':[{'id':c,'category':c,'status':'pass','evidence':'Technical test fixture only; not real creative or provider validation.'} for c in ['playback','timing','visual','audio','captions','continuity','claims']]}
        atomic_json(self.root/'project.json',p)
        atomic_json(self.root/'09_QA/qa.json',{'video_sha256':self.result['sha256'],'checks':[{'category':c,'status':'pass','evidence':'Technical fixture, not a real content review.'} for c in ['story','identity','visual','audio','captions']]})
        h=self.root/'.history/events';h.mkdir(parents=True);(h/'0001.md').write_text('Technical fixture history, not production evidence.')
    def tearDown(self):self.tmp.cleanup()
    def test_real_file_and_original_package_contract_validate(self):
        result=self.runner.validate_delivery(self.root,self.req)
        self.assertEqual(result['sha256'],self.result['sha256'])
    def test_unverified_audio_cannot_be_finalized(self):
        path=self.root/'09_QA/qa.json';qa=json.loads(path.read_text());qa['checks'][3]['status']='unverified';atomic_json(path,qa)
        with self.assertRaises(Problem):self.runner.validate_delivery(self.root,self.req)
    def test_old_canonical_final_asset_cannot_be_finalized(self):
        path=self.root/'project.json';p=json.loads(path.read_text());p['asset_registry'][0]['path']='speech.wav';atomic_json(path,p)
        with self.assertRaises(Problem):self.runner.validate_delivery(self.root,self.req)

if __name__=='__main__':unittest.main()
