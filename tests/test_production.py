import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from server.production import inspect,validate
from server.store import Problem


class ProductionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'06_GENERATION_SPECS').mkdir();(self.root/'boards').mkdir()
        self.project={'project_id':'P1','scenes':[{'id':'S01'}],'shots':[{'id':'SH01','scene_id':'S01','start_s':0,'end_s':3}]}
        (self.root/'project.json').write_text(json.dumps(self.project))
        Image.new('RGB',(30,50),'blue').save(self.root/'boards/sheet.png')
        self.data={'schema_version':1,'project_id':'P1','source_shots_sha256':hashlib.sha256(json.dumps(self.project['shots'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
                   'scenes':[{'scene_id':'S01','storyboard_sheets':[{'path':'boards/sheet.png','sha256':hashlib.sha256((self.root/'boards/sheet.png').read_bytes()).hexdigest(),'shot_ids':['SH01']}]}],
                   'shots':[{'shot_id':'SH01','scene_id':'S01','duration_s':3,'intent':'Reveal the empty bowl','first_frame_prompt':'Close view of the office cat eyes with the established scarf.','last_frame_prompt':'Wider frame reveals the empty bowl below, same identity and lighting.','motion_prompt':'Motivated tilt down, reveal bowl, settle for reaction.','control_level':'SOFT_PROMPT','implementation_status':'planned','camera':{k:k+' actual scene intent' for k in ('shot_size','angle','composition','movement','motivation','rhythm','continuity')},'timing':[{'start_s':0,'end_s':1,'action':'Anticipation hold'},{'start_s':1,'end_s':3,'action':'Reveal, settle, cut'}]}]}
    def tearDown(self):self.tmp.cleanup()
    def write(self): (self.root/'06_GENERATION_SPECS/production_handoff.json').write_text(json.dumps(self.data))
    def test_real_complete_sheet_and_frame_package(self):self.write();self.assertTrue(validate(self.root)['valid'])
    def test_cannot_replace_a_sheet_with_missing_file_or_fake_hash(self):
        self.data['scenes'][0]['storyboard_sheets'][0]['sha256']='fake';self.write();self.assertFalse(inspect(self.root)['valid'])
        (self.root/'boards/sheet.png').unlink();self.assertFalse(inspect(self.root)['valid'])
    def test_every_canonical_cut_needs_both_endpoints_and_camera_rhythm(self):
        del self.data['shots'][0]['last_frame_prompt'];del self.data['shots'][0]['camera']['rhythm'];self.write()
        errors=inspect(self.root)['errors'];self.assertTrue(any('last_frame_prompt' in x for x in errors));self.assertTrue(any('rhythm' in x for x in errors))
    def test_stale_source_shots_are_detected(self):
        self.write();self.project['shots'][0]['end_s']=4;(self.root/'project.json').write_text(json.dumps(self.project));self.assertFalse(inspect(self.root)['valid'])
    def test_sheet_coverage_and_scene_membership_are_checked(self):
        self.data['scenes'][0]['storyboard_sheets'][0]['shot_ids']=['OTHER'];self.write();self.assertFalse(inspect(self.root)['valid'])
    def test_timing_is_contiguous_and_matches_measured_duration(self):
        self.data['shots'][0]['timing'][1]['start_s']=1.5;self.write();self.assertFalse(inspect(self.root)['valid'])
    def test_unverified_camera_text_is_never_hard_control(self):
        self.data['shots'][0]['control_level']='HARD_CONTROL';self.write();self.assertFalse(inspect(self.root)['valid'])
    def test_sheet_path_cannot_escape_project(self):
        self.data['scenes'][0]['storyboard_sheets'][0]['path']='../outside.png';self.write();self.assertFalse(inspect(self.root)['valid'])
    def test_missing_handoff_is_not_a_completed_delivery(self):
        self.assertFalse(inspect(self.root)['available'])
        with self.assertRaises(Problem):validate(self.root)
    def test_endpoint_prompt_cannot_be_a_same_as_above_placeholder(self):
        self.data['shots'][0]['last_frame_prompt']='same as above';self.write();self.assertFalse(inspect(self.root)['valid'])
    def test_scene_endpoints_come_from_canonical_first_and_last_cuts(self):
        self.write();scene=inspect(self.root)['scenes'][0]
        self.assertEqual(scene['first_shot_id'],'SH01')
        self.assertEqual(scene['first_frame_prompt'],self.data['shots'][0]['first_frame_prompt'])
        self.assertEqual(scene['last_frame_prompt'],self.data['shots'][0]['last_frame_prompt'])
