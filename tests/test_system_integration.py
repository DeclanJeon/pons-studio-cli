import json,os,tempfile,tomllib,unittest
from pathlib import Path
from unittest.mock import patch
from server.store import Store,Problem,atomic_json
from server.runner import Runner
from server import system_integration as system
from server import google

class SystemIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.home=self.root/'operator';self.home.mkdir();self.codex=self.home/'.codex';self.codex.mkdir()
        self.config='''model="existing-model"
[mcp_servers.fish-audio]
url="https://api.fish.audio/mcp"
[mcp_servers.elevenlabs]
url="https://api.us.elevenlabs.io/v1/mcp"
[mcp_servers.unrelated]
url="https://example.com/mcp"
[plugins."unrelated@example"]
enabled=true
'''
        (self.codex/'config.toml').write_text(self.config);(self.codex/'auth.json').write_text('PRIVATE_SYSTEM_AUTH')
        self.store=Store(self.root/'data');self.runner=object.__new__(Runner);self.runner.store=self.store
        self.env=patch.dict(os.environ,{'STUDIO_SYSTEM_CODEX_HOME':str(self.codex)});self.env.start()
        self.homepatch=patch('pathlib.Path.home',return_value=self.home);self.homepatch.start()
        self.whichpatch=patch.object(system.shutil,'which',return_value=None);self.whichpatch.start()
    def tearDown(self):self.whichpatch.stop();self.homepatch.stop();self.env.stop();self.tmp.cleanup()
    def test_explicit_system_import_reuses_auth_and_is_idempotent(self):
        first=system.import_system(self.store);ids=[c['id'] for c in self.store.connections()]
        system.import_system(self.store)
        self.assertEqual(ids,[c['id'] for c in self.store.connections()]);self.assertEqual(len(ids),4)
        self.assertEqual(self.runner.env()['CODEX_HOME'],str(self.codex))
        self.assertFalse((self.store.root/'codex/auth.json').exists())
        self.assertEqual((self.codex/'config.toml').read_text(),self.config)
        self.assertNotIn('PRIVATE_SYSTEM_AUTH',json.dumps(first))
    def test_resync_preserves_verified_status_for_unchanged_connections(self):
        system.import_system(self.store);c=self.store.connections()[0];self.store.set_connection_status(c['id'],'verified')
        system.import_system(self.store);self.assertEqual(self.store.connection(c['id'])['status'],'verified')
    def test_installed_higgsfield_cli_is_a_distinct_selectable_connection(self):
        with patch.object(system.shutil,'which',return_value='/installed/higgsfield'):system.import_system(self.store)
        c=next(c for c in self.store.connections() if c['transport']=='cli')
        self.assertEqual(c['config']['command'],['/installed/higgsfield']);self.assertEqual(c['status'],'ready')
    def test_execution_config_disables_unselected_tools_and_preserves_names(self):
        system.import_system(self.store);args=self.runner.config_args()
        parsed={}
        for i in range(0,len(args),2):parsed.update(tomllib.loads(args[i+1]))
        self.assertFalse(parsed['mcp_servers']['fish-audio']['enabled'])
        self.assertFalse(parsed['mcp_servers']['unrelated']['enabled'])
        self.assertFalse(parsed['plugins']['unrelated@example']['enabled'])
        self.assertFalse(parsed['features']['apps']);self.assertFalse(parsed['features']['plugins'])
        self.assertFalse(parsed['features']['skill_mcp_dependency_install'])
        self.assertEqual(parsed['model'],'existing-model')
        self.assertEqual((self.codex/'config.toml').read_text(),self.config)
    def test_config_secrets_are_env_references_not_argv(self):
        secret='SENSITIVE_HEADER_VALUE'
        with (self.codex/'config.toml').open('a') as f:f.write('\n[mcp_servers.fish-audio.http_headers]\nAuthorization="'+secret+'"\n')
        system.import_system(self.store);args=self.runner.config_args()
        self.assertNotIn(secret,' '.join(args));self.assertIn(secret,self.runner.env().values())
        self.assertNotIn(secret,(self.store.root/'codex/config.toml').read_text())
    def test_isolated_default_keeps_credentials_separate(self):
        self.assertFalse(self.runner.system_mode());self.assertEqual(self.runner.env()['CODEX_HOME'],str(self.store.root/'codex'))
    def test_typecast_env_is_data_never_executed(self):
        p=self.home/'.config/typecast/env';p.parent.mkdir(parents=True)
        key='literal_$(touch SHOULD_NOT_EXIST)'
        p.write_text("export TYPECAST_API_KEY='"+key+"'\n")
        system.import_system(self.store)
        c=next(c for c in self.store.connections() if c['provider']=='typecast')
        self.assertEqual(self.store.connection(c['id'],True)['secret'],key)
        self.assertNotIn(key,json.dumps(self.store.connections()))
        self.assertFalse((Path.cwd()/'SHOULD_NOT_EXIST').exists())
    def google_row(self,kind='googleDriveOAuth2Api'):
        return {'id':'sample','type':kind,'data':{'clientId':'id','clientSecret':'CLIENT_PRIVATE','oauthTokenData':{'refresh_token':'REFRESH_PRIVATE','scope':'https://www.googleapis.com/auth/drive'}}}
    def test_n8n_import_encrypts_secrets_and_preserves_origin(self):
        with patch.object(system,'export_n8n',return_value=self.google_row()):
            conn=system.import_google(self.store,'sample')
            again=system.import_google(self.store,'sample')
        self.assertEqual(conn['id'],again['id']);self.assertEqual(conn['config']['source'],'n8n')
        self.assertNotIn('REFRESH_PRIVATE',json.dumps(conn))
        self.assertNotIn(b'REFRESH_PRIVATE',(self.store.root/'studio.sqlite').read_bytes())
    def test_n8n_import_rejects_other_credential_types(self):
        with patch.object(system,'export_n8n',return_value=self.google_row('httpHeaderAuth')):
            with self.assertRaises(Problem):system.import_google(self.store,'sample')
    def test_n8n_ids_cannot_inject_shell_commands(self):
        with patch.object(system.subprocess,'run') as call:
            with self.assertRaises(Problem):system.export_n8n('sample; touch nope')
            call.assert_not_called()
    def test_google_verification_is_read_only(self):
        with patch.object(system,'export_n8n',return_value=self.google_row()):c=system.import_google(self.store,'sample')
        with patch.object(google,'access_token',return_value='ACCESS_PRIVATE'),patch.object(google,'jsoncall',return_value={}) as read,patch.object(google,'call',return_value=(404,{},b'')) as probe:
            caps=google.verify_connection(self.store,c['id'])
        self.assertEqual(caps,{'drive':True,'sheets':True});self.assertIsNone(read.call_args.kwargs.get('body'))
        self.assertEqual(len(probe.call_args.args),2)
    def test_youtube_verification_never_enables_publication(self):
        with patch.object(system,'export_n8n',return_value=self.google_row('youTubeOAuth2Api')):c=system.import_google(self.store,'sample')
        with patch.object(google,'access_token',return_value='ACCESS_PRIVATE'),patch.object(google,'jsoncall',return_value={'items':[{'id':'channel'}]}) as read:
            caps=google.verify_connection(self.store,c['id'])
        self.assertFalse(caps['publication_enabled']);self.assertIn('mine=true',read.call_args.args[0])
    def test_refresh_rotation_is_encrypted_and_only_refresh_fields_sent(self):
        with patch.object(system,'export_n8n',return_value=self.google_row()):c=system.import_google(self.store,'sample')
        with patch.object(google,'call',return_value=(200,{},json.dumps({'access_token':'ACCESS_PRIVATE','refresh_token':'ROTATED_PRIVATE'}).encode())) as call:
            self.assertEqual(google.access_token(self.store,c['id']),'ACCESS_PRIVATE')
        data=json.loads(self.store.connection(c['id'],True)['secret'])
        self.assertEqual(data['refresh_token'],'ROTATED_PRIVATE');self.assertNotIn(b'ROTATED_PRIVATE',(self.store.root/'studio.sqlite').read_bytes())
        self.assertNotIn(b'source_key',call.call_args.kwargs['body'])

if __name__=='__main__':unittest.main()

class DependencyTests(unittest.TestCase):
    def test_only_actual_named_module_satisfies_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'data');source=store.root/'skills/source/creative-production/SKILL.md'
            source.parent.mkdir(parents=True);source.write_text('REQUIRED SUB-SKILL: `ai-delegation-loop`')
            home=Path(temp)/'home';home.mkdir()
            with patch('pathlib.Path.home',return_value=home):
                self.assertEqual(system.missing_dependencies(store),['ai-delegation-loop'])
                module=home/'.agents/skills/ai-delegation-loop/SKILL.md';module.parent.mkdir(parents=True)
                module.write_text('---\nname: some-other-module\n---\n')
                self.assertEqual(system.missing_dependencies(store),['ai-delegation-loop'])
                module.write_text('---\nname: ai-delegation-loop\n---\nActual test module.')
                self.assertEqual(system.missing_dependencies(store),[])
                self.assertEqual(system.coordination_skill(store),module.resolve())
    def test_google_inner_tokens_are_redacted_from_agent_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(temp)
            store.save_connection({'name':'google','provider':'google','transport':'api','role':'storage','api_key':json.dumps({'refresh_token':'PRIVATE_REFRESH_123','client_secret':'PRIVATE_CLIENT_456'})})
            runner=object.__new__(Runner);runner.store=store
            self.assertNotIn('PRIVATE_REFRESH_123',runner.redact('error token PRIVATE_REFRESH_123 and PRIVATE_CLIENT_456'))
            self.assertNotIn('PRIVATE_CLIENT_456',runner.redact('error token PRIVATE_REFRESH_123 and PRIVATE_CLIENT_456'))

class MCPAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.runner=object.__new__(Runner);self.runner.store=self.store
        self.image=self.store.save_connection({'name':'Fish image','provider':'fish','role':'image','transport':'system_mcp','config':{'system_mcp_name':'fish-audio'}})
        self.speech=self.store.save_connection({'name':'Fish speech','provider':'fish','role':'speech','transport':'system_mcp','config':{'system_mcp_name':'fish-audio'}})
    def tearDown(self):self.temp.cleanup()
    def parsed(self,request):
        args,policy=self.runner.mcp_execution_args(request);result={}
        for index in range(0,len(args),2):
            import copy
            def merge(a,b):
                for k,v in b.items():
                    if isinstance(v,dict):merge(a.setdefault(k,{}),v)
                    else:a[k]=v
            merge(result,tomllib.loads(args[index+1]))
        return result['mcp_servers']['fish-audio'],policy
    def test_selected_image_grant_cannot_clone_speak_publish_or_generate_video(self):
        config,policy=self.parsed({'connections':{'image':self.image['id']},'execution_authorized':True})
        self.assertEqual(config['tools']['generate_image']['approval_mode'],'approve')
        self.assertEqual(config['default_tools_approval_mode'],'auto')
        for tool in ['create_voice_clone','text_to_speech','generate_video','update_asset','studio_generate']:
            self.assertNotIn(tool,config['enabled_tools']);self.assertNotIn(tool,config['tools'])
    def test_no_execution_grant_means_no_tool_preapproval(self):
        config,policy=self.parsed({'connections':{'image':self.image['id']},'execution_authorized':False})
        self.assertNotIn('tools',config);self.assertFalse(policy['fish-audio']['preauthorized'])
    def test_shared_server_combines_only_selected_roles(self):
        config,policy=self.parsed({'connections':{'image':self.image['id'],'speech':self.speech['id']},'execution_authorized':True})
        self.assertIn('text_to_speech',config['enabled_tools']);self.assertIn('generate_image',config['enabled_tools'])
        self.assertNotIn('create_voice_clone',config['enabled_tools'])
    def test_unknown_provider_is_never_given_blanket_write_approval(self):
        connection=self.store.save_connection({'name':'Other','provider':'custom','role':'image','transport':'mcp','config':{'url':'https://example.com/mcp'}})
        args,policy=self.runner.mcp_execution_args({'connections':{'image':connection['id']},'execution_authorized':True})
        self.assertFalse(policy[connection['id']]['preauthorized'])
        self.assertFalse(any('approval_mode="approve"' in arg for arg in args))
    def test_real_codex_parser_accepts_scoped_overrides(self):
        import shutil,subprocess
        if not shutil.which('codex'):self.skipTest('Codex CLI unavailable')
        args,policy=self.runner.mcp_execution_args({'connections':{'image':self.image['id']},'execution_authorized':True})
        env={**os.environ,'CODEX_HOME':str(self.store.root/'codex')}
        result=subprocess.run(['codex','-c','mcp_servers={fish-audio={url="https://example.com/mcp",enabled=false}}',*args,'mcp','list','--json'],env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(any(row['name']=='fish-audio' for row in json.loads(result.stdout)))

class ArchiveFolderReuseTests(unittest.TestCase):
    def test_archive_ensures_each_parent_once_but_still_verifies_every_file(self):
        import itertools
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/'data');root=Path(td)/'project';(root/'one/two').mkdir(parents=True)
            for name in ('a.txt','b.txt','c.txt'):(root/'one/two'/name).write_text(name)
            ids=('id'+str(i) for i in itertools.count())
            with patch.object(google,'access_token',return_value='test-token'),patch.object(google,'allocate',side_effect=lambda token:next(ids)),patch.object(google,'create_folder') as folder,patch.object(google,'upload') as upload,patch.object(google,'jsoncall',side_effect=[{'spreadsheetId':'sheet-test'},{}]):
                result=google.archive(store,'conn-test','prj-test',root,{'duration_s':30})
            self.assertEqual(folder.call_count,3) # project, one, one/two
            self.assertEqual(upload.call_count,3)
            self.assertEqual(result['file_count'],3)
