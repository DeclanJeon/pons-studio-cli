import copy
import hashlib
import json
import unittest
from unittest.mock import patch
import test_production as fixtures
import test_core as core
from server.storyboard import timeline,markdown,write_export,EXPORT
from server.production import inspect
from server.store import Problem


class StoryboardTests(unittest.TestCase):
    setUp=fixtures.ProductionHandoffTests.setUp
    tearDown=fixtures.ProductionHandoffTests.tearDown
    write=fixtures.ProductionHandoffTests.write

    def test_global_order_connections_and_disjoint_scenes(self):
        data={'fps':24,'scenes':[{'id':'A'},{'id':'B'}],'shots':[
            {'id':'3','scene_id':'A','start_s':5,'end_s':7},
            {'id':'1','scene_id':'A','start_s':0,'end_s':2},
            {'id':'2','scene_id':'B','start_s':2,'end_s':5}]}
        result=timeline(data)
        self.assertTrue(result['valid']);self.assertEqual(result['total_duration_s'],7)
        self.assertEqual([x['shot_id'] for x in result['shots']],['1','2','3'])
        self.assertEqual(result['shots'][1]['start_frame'],48)
        self.assertEqual(result['shots'][1]['previous_shot_id'],'1')
        self.assertEqual(result['shots'][1]['next_shot_id'],'3')
        self.assertEqual(len(result['scenes'][0]['intervals']),2)
        self.assertEqual(result['scenes'][0]['duration_s'],4)

    def test_invalid_timing_never_looks_complete(self):
        for start,end in [(1,3),(0,0),(0,float('inf')),(0,True)]:
            data=copy.deepcopy(self.project);data['shots'][0].update(start_s=start,end_s=end)
            self.assertFalse(timeline(data)['valid'])
        for start in [2,4]:
            data=copy.deepcopy(self.project);data['shots'].append({'id':'2','scene_id':'S01','start_s':start,'end_s':6})
            self.assertFalse(timeline(data)['valid'])

    def test_draft_has_timeline_without_claiming_sheets_or_prompts(self):
        result=inspect(self.root);self.assertFalse(result['valid']);self.assertFalse(result['available'])
        self.assertTrue(result['timeline']['valid'])
        text=markdown(self.root)
        for expected in ['미완성 설계 초안','00:00:00.000','00:00:03.000','첫 프레임 프롬프트','미작성','씬 타임라인','컷 타임라인']:self.assertIn(expected,text)

    def test_full_document_projects_local_phases_to_global_time(self):
        second=copy.deepcopy(self.data['shots'][0]);second.update(shot_id='SH02')
        self.project['shots'].append({'id':'SH02','scene_id':'S01','start_s':3,'end_s':6,'purpose':'reveal | hold\nend'})
        self.data['shots'].append(second);self.data['scenes'][0]['storyboard_sheets'][0]['shot_ids'].append('SH02')
        self.data['source_shots_sha256']=hashlib.sha256(json.dumps(self.project['shots'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        (self.root/'project.json').write_text(json.dumps(self.project));self.write()
        self.assertTrue(inspect(self.root)['valid']);text=markdown(self.root)
        for expected in ['00:00:03.000–00:00:04.000','../boards/sheet.png','reveal \\| hold<br>end',second['first_frame_prompt'],second['last_frame_prompt'],'SH02 @ 00:00:03.000']:self.assertIn(expected,text)
        self.assertEqual(inspect(self.root)['scenes'][0]['storyboard_sheets'][0]['timeline'][1]['start_s'],3)

    def test_editorial_reuse_is_derived_with_distinct_authority(self):
        self.project['editorial_reuse']=[{'shot_id':'SH01','donor_shot_id':'SH00','reason':'hold | continuity','authority':'coordinator judgment; no user acceptance'}]
        (self.root/'project.json').write_text(json.dumps(self.project))
        text=markdown(self.root)
        self.assertIn('SH00',text);self.assertIn('hold \\| continuity',text)
        self.assertIn('coordinator judgment; no user acceptance',text)

    def test_atomic_export_preserves_canonical(self):
        before=(self.root/'project.json').read_bytes();result=write_export(self.root)
        self.assertEqual(result['path'],EXPORT);self.assertEqual(before,(self.root/'project.json').read_bytes())
        self.assertEqual(result['sha256'],hashlib.sha256((self.root/EXPORT).read_bytes()).hexdigest())
        self.assertFalse(list((self.root/EXPORT).parent.glob('*.tmp')))

    def test_malformed_canonical_does_not_crash_gallery(self):
        (self.root/'project.json').write_text('[]')
        self.assertFalse(inspect(self.root)['timeline']['available'])
        with self.assertRaises(Problem):markdown(self.root)

    def test_concurrent_revision_download_rejected(self):
        def change(root):
            (self.root/'project.json').write_text('{}');return 'mixed'
        with patch('server.storyboard._markdown',side_effect=change),self.assertRaises(Problem) as error:markdown(self.root)
        self.assertEqual(error.exception.status,409)


class StoryboardHTTPTests(unittest.TestCase):
    setUp=core.HTTPTests.setUp
    tearDown=core.HTTPTests.tearDown
    call=core.HTTPTests.call

    def test_download_is_authenticated_utf8_markdown_and_draft_aware(self):
        job=self.app.store.create(core.request(),'timeline')
        root=self.app.store.project_root(job['id']);root.mkdir()
        (root/'project.json').write_text(json.dumps({'project_id':job['id'],'scenes':[{'id':'S1'}],'shots':[{'id':'C1','scene_id':'S1','start_s':0,'end_s':5}]}))
        url=f'/api/projects/{job["id"]}/storyboard-md'
        self.assertEqual(self.call(url,auth=False)[0],401)
        status,headers,body=self.call(url)
        self.assertEqual(status,200);self.assertIn('text/markdown',headers['Content-Type'])
        self.assertIn('attachment;',headers['Content-Disposition']);self.assertIn('미완성 설계 초안',body.decode())
        self.assertFalse((root/EXPORT).exists())
