import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server.store import Store, Problem, atomic_json
from server.runner import Runner, execution_prompt
from server import system_integration as system


class DefaultImageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'data')
        self.runner=object.__new__(Runner)
        self.runner.store=self.store
        self.runner.auth_sessions={}
        self.speech=self.store.save_connection({'name':'speech','provider':'typecast','role':'speech','transport':'api'})
        self.store.set_connection_status(self.speech['id'],'verified')
        self.image=self.store.save_connection({'name':'image','provider':'fish','role':'image','transport':'system_mcp','config':{'system_mcp_name':'fish-audio'}})
        self.store.set_connection_status(self.image['id'],'ready')
        self.request={'user_prompt':'원래 요청 그대로','target_duration_s':60,'aspect_ratio':'16:9','connections':{'speech':self.speech['id']},'execution_authorized':True,'limits':{'wall_time_s':120,'max_tts_characters':1000,'max_tts_calls':2}}
        self.status={'codex_authenticated':True,'skills':True,'ffmpeg':True,'ffprobe':True,'codex_imagen':{'skill_found':True,'native_runtime_available':True}}

    def tearDown(self):self.temp.cleanup()

    def test_default_is_skill_even_when_external_images_exist(self):
        self.assertEqual(system.image_executor(self.request),'codex-imagen')
        with patch.object(self.runner,'status',return_value=self.status):self.runner.preflight(self.request)
        self.assertNotIn('image',self.request['connections'])

    def test_missing_skill_blocks_instead_of_switching_provider(self):
        self.status['codex_imagen']['skill_found']=False
        with patch.object(self.runner,'status',return_value=self.status), self.assertRaises(Problem) as failure:
            self.runner.preflight(self.request)
        self.assertIn('codex-imagen',failure.exception.message)

    def test_missing_native_runtime_blocks_default(self):
        self.status['codex_imagen']['native_runtime_available']=False
        with patch.object(self.runner,'status',return_value=self.status), self.assertRaises(Problem):
            self.runner.preflight(self.request)

    def test_explicit_provider_does_not_need_default_skill(self):
        self.request['connections']['image']=self.image['id']
        self.status['codex_imagen']={}
        with patch.object(self.runner,'status',return_value=self.status):self.runner.preflight(self.request)
        args,policy=self.runner.mcp_execution_args(self.request)
        self.assertIn('features.image_generation=false',args)
        self.assertIn('fish-audio',policy)

    def test_default_native_grant_does_not_enable_external_image_tools(self):
        self.request['connections']['image']='codex-imagen'
        args,policy=self.runner.mcp_execution_args(self.request)
        self.assertIn('features.image_generation=true',args)
        self.assertEqual(policy,{})
        self.assertEqual(system.connection_ids(self.request),{self.speech['id']})
        self.request['execution_authorized']=False
        self.assertIn('features.image_generation=false',self.runner.mcp_execution_args(self.request)[0])

    def test_null_default_does_not_enter_connection_lookup(self):
        self.request['connections']['image']=None
        self.assertEqual(system.connection_ids(self.request),{self.speech['id']})
        self.runner.mcp_execution_args(self.request)

    def test_real_named_skill_is_linked_and_catalogued_without_credentials(self):
        skill=self.root/'external/codex-imagen'
        skill.mkdir(parents=True)
        (skill/'SKILL.md').write_text('---\nname: codex-imagen\n---\nImage craft instructions.')
        atomic_json(self.store.root/'runtime.json',{'additional_skill_paths':[str(skill/'SKILL.md')]})
        self.assertEqual(system.imagen_skill(self.store),skill/'SKILL.md')
        (self.store.root/'skills/source').mkdir()
        job=self.store.create(self.request,'test')
        with patch.object(system,'coordination_skill',return_value=None):root=self.runner.prepare(job)
        self.assertEqual((root/'.agents/skills/codex-imagen').resolve(),skill)
        catalog=json.loads((root/'connections.json').read_text())
        self.assertEqual({c['id'] for c in catalog},{self.speech['id'],'codex-imagen'})
        self.assertNotIn('secret',json.dumps(catalog))
        self.assertEqual(json.loads((root/'request.json').read_text())['user_prompt'],self.request['user_prompt'])

    def test_wrong_named_skill_cannot_masquerade_as_default(self):
        path=self.root/'wrong/SKILL.md';path.parent.mkdir();path.write_text('---\nname: another-skill\n---')
        atomic_json(self.store.root/'runtime.json',{'additional_skill_paths':[str(path)]})
        with patch('pathlib.Path.home',return_value=self.root/'empty'):
            self.assertIsNone(system.imagen_skill(self.store))

    def test_prompt_has_default_native_adapter_and_explicit_override(self):
        job={'id':'prj_test','request':self.request}
        prompt=execution_prompt(self.root,job)
        self.assertIn('Default still-image route: codex-imagen',prompt)
        self.assertIn('do not run its OAuth helper',prompt)
        self.assertIn(self.request['user_prompt'],prompt)
        self.request['connections']['image']=self.image['id']
        prompt=execution_prompt(self.root,job)
        self.assertIn('Explicit image-provider override',prompt)
        self.assertNotIn('Default still-image route:',prompt)
