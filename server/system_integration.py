"""Explicit local-operator integration; credentials never enter the public catalog."""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from .store import Problem, atomic_json

PROVIDERS={'fish-audio':('fish','Fish Audio',['speech','image']),
           'elevenlabs':('elevenlabs','ElevenLabs',['speech','image']),
           'higgsfield':('custom','Higgsfield',['image']),
           'typecast':('typecast','Typecast',['speech'])}
SUPPORT=('fetch','linux-video-editor')
DEFAULT_IMAGE_EXECUTOR='codex-imagen'

def image_executor(request):
    return request.get('connections',{}).get('image') or DEFAULT_IMAGE_EXECUTOR

def connection_ids(request):
    return {cid for role,cid in request.get('connections',{}).items()
            if cid and (role!='video' or request.get('motion_video_authorized') is True and request.get('delivery_kind')=='video_edit') and not (role=='image' and cid==DEFAULT_IMAGE_EXECUTOR)}

def imagen_skill(store):
    paths=[Path(p) for p in runtime(store).get('additional_skill_paths',[])]
    paths += [store.root/'skills/source/codex-imagen/SKILL.md']
    paths += [Path.home()/root/'skills/codex-imagen/SKILL.md' for root in ('.agents','.codex')]
    for path in paths:
        if path.is_dir():path=path/'SKILL.md'
        if path.is_file():
            import re
            if re.search(r'^name:\s*[\"\']?codex-imagen[\"\']?\s*$',path.read_text(),re.M):return path.resolve()
    return None
FISH_READ={'get_credit_balance','get_media_model','list_media_models','get_generation_status','get_generation_result','get_asset','estimate_image_generation','estimate_image_edit'}
FISH_IMAGE=FISH_READ|{'generate_image','generate_image_edit','create_media_upload','get_upload_url','speech_to_text'}
FISH_SPEECH={'get_credit_balance','search_voices','get_voice','text_to_speech','get_upload_url','speech_to_text'}
ELEVEN_COMMON={'creative_create_flow','creative_get_flow','creative_get_flow_node','creative_get_flow_run_status','creative_get_generations','creative_get_model_schema','creative_get_model_guide','creative_get_flow_node_types','creative_create_asset_upload','creative_finalize_asset_upload','creative_attach_reference_file','creative_transcribe_audio'}

def authorized_mcp_tools(provider,roles):
    tools=set()
    if provider=='fish':
        if 'image' in roles:tools|=FISH_IMAGE
        if 'speech' in roles:tools|=FISH_SPEECH
    elif provider=='elevenlabs':
        if 'image' in roles:tools|=ELEVEN_COMMON|{'creative_generate_image','creative_edit_image'}
        if 'speech' in roles:tools|=ELEVEN_COMMON|{'creative_list_voices','creative_generate_speech'}
    return sorted(tools)

def runtime(store):
    path=store.root/'runtime.json'
    return json.loads(path.read_text()) if path.exists() else {}

def system_home(store=None):
    cfg=runtime(store) if store else {}
    return Path(cfg.get('system_codex_home') or os.environ.get('STUDIO_SYSTEM_CODEX_HOME') or os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().resolve()

def system_config(store=None):
    try:return tomllib.loads((system_home(store)/'config.toml').read_text())
    except FileNotFoundError:return {}  # Codex credentials can exist without an optional config.
    except (OSError,ValueError):raise Problem('이 컴퓨터의 Codex 설정 파일을 읽을 수 없습니다.')

def coordination_skill(store):
    paths=[Path(p) for p in runtime(store).get('additional_skill_paths',[])]
    paths += [Path.home()/root/'skills/ai-delegation-loop/SKILL.md' for root in ('.agents','.codex','.claude')]
    cache=Path.home()/'.codex/plugins/cache'
    if cache.is_dir():paths.extend(cache.glob('**/ai-delegation-loop/SKILL.md'))
    for path in paths:
        if path.is_dir():path=path/'SKILL.md'
        if path.is_file():
            import re
            if re.search(r'^name:\s*[\"\']?ai-delegation-loop[\"\']?\s*$',path.read_text(),re.M):return path.resolve()
    return None

def missing_dependencies(store):
    source=store.root/'skills/source/creative-production/SKILL.md'
    return ['ai-delegation-loop'] if source.is_file() and '`ai-delegation-loop`' in source.read_text() and not coordination_skill(store) else []

def toml_value(value):
    if isinstance(value,dict):return '{'+', '.join(json.dumps(str(k))+ ' = '+toml_value(v) for k,v in value.items())+'}'
    if isinstance(value,list):return '['+', '.join(toml_value(v) for v in value)+']'
    if isinstance(value,(str,int,float,bool)):return json.dumps(value,ensure_ascii=False)
    raise Problem('지원하지 않는 Codex 설정 값입니다.')

def upsert(store,source,**data):
    with store.lock:
        old=next((c for c in store.connections() if c['config'].get('source_key')==source),None)
        data.setdefault('config',{})['source_key']=source
        if not old:return store.save_connection(data)
        token=data.get('api_key')
        if old['config']==data['config'] and old['name']==data['name']:
            previous=store.connection(old['id'],True).get('secret','')
            if previous==(token or ''):return old
        with store.db() as db:
            db.execute('UPDATE connections SET name=?,config=?,secret=?,status=?,checked_at=NULL WHERE id=?',
              (data['name'],json.dumps(data['config']),store.vault.encrypt(token.encode()) if token else None,'saved',old['id']))
        return store.connection(old['id'])

def import_system(store):
    cfg=system_config(store);home=system_home(store)
    report={'imported':[],'missing':[],'codex_mode':'system'}
    settings=runtime(store)
    settings.update({'codex_mode':'system','system_codex_home':str(home)})
    atomic_json(store.root/'runtime.json',settings);os.chmod(store.root/'runtime.json',0o600)
    servers=cfg.get('mcp_servers',{})
    for name,(provider,label,roles) in PROVIDERS.items():
        spec=servers.get(name)
        if not spec or spec.get('enabled') is False:
            report['missing'].append(name);continue
        for role in roles:
            conn=upsert(store,'system-mcp:'+name+':'+role,name=label+' · '+('음성' if role=='speech' else '이미지'),provider=provider,transport='system_mcp',role=role,config={'system_mcp_name':name,'source':'system'})
            if conn['status']=='saved':store.set_connection_status(conn['id'],'ready')
            report['imported'].append({'id':conn['id'],'name':conn['name'],'role':role})
    key_path=Path.home()/'.config/typecast/env'
    if key_path.is_file():
        # Parse data only. Never source or execute the user's credential file.
        key=None
        for line in key_path.read_text().splitlines():
            line=line.strip().removeprefix('export ')
            if line.startswith('TYPECAST_API_KEY='):
                values=shlex.split(line.split('=',1)[1],comments=True)
                if len(values)==1:key=values[0]
        if key:
            conn=upsert(store,'system-api:typecast',name='Typecast · 음성',provider='typecast',transport='api',role='speech',config={'source':'system'},api_key=key)
            report['imported'].append({'id':conn['id'],'name':conn['name'],'role':'speech'})
            report['missing']=[x for x in report['missing'] if x!='typecast']
    executable=shutil.which('higgsfield')
    if executable:
        conn=upsert(store,'system-cli:higgsfield',name='Higgsfield CLI · 이미지',provider='custom',transport='cli',role='image',config={'command':[executable],'source':'system'})
        if conn['status']=='saved':store.set_connection_status(conn['id'],'ready')
        report['imported'].append({'id':conn['id'],'name':conn['name'],'role':'image'})
        video=upsert(store,'system-cli:higgsfield:video',name='Higgsfield CLI · 영상',provider='custom',transport='cli',role='video',config={'command':[executable],'source':'system','runtime':'higgsfield'})
        if video['status']=='saved':store.set_connection_status(video['id'],'ready')
        report['imported'].append({'id':video['id'],'name':video['name'],'role':'video'})
    return report

def export_n8n(credential_id,host='pons'):
    if not credential_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in credential_id):raise Problem('n8n 연결 ID가 올바르지 않습니다.')
    # Export one authorized credential to a private, short-lived container file.
    script='f=$(mktemp /tmp/pons-credential.XXXXXX); chmod 600 "$f"; trap \'rm -f "$f"\' EXIT; n8n export:credentials --id='+shlex.quote(credential_id)+' --decrypted --output="$f" >/dev/null 2>&1 && cat "$f"'
    cmd='docker exec n8n sh -lc '+shlex.quote(script)
    try:r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,cmd],capture_output=True,text=True,timeout=90)
    except (OSError,subprocess.TimeoutExpired):raise Problem('pons의 n8n 연결 정보를 가져오지 못했습니다.',502)
    if r.returncode:raise Problem('pons의 n8n 연결 정보를 가져오지 못했습니다.',502)
    try:
        rows=json.loads(r.stdout)
        if len(rows)!=1 or rows[0]['id']!=credential_id:raise ValueError()
        return rows[0]
    except (ValueError,KeyError,TypeError):raise Problem('n8n 연결 정보의 형식을 확인할 수 없습니다.',502)

def import_google(store,credential_id,host='pons'):
    row=export_n8n(credential_id,host)
    provider={'googleDriveOAuth2Api':'google','youTubeOAuth2Api':'youtube'}.get(row['type'])
    if not provider:raise Problem('Drive 또는 YouTube OAuth 연결만 가져올 수 있습니다.')
    data=row['data'];token=data.get('oauthTokenData',{})
    if isinstance(token,str):token=json.loads(token)
    secret={'client_id':data.get('clientId'),'client_secret':data.get('clientSecret'),'refresh_token':token.get('refresh_token')}
    if not all(secret.values()):raise Problem('n8n 연결에 재사용 가능한 OAuth 정보가 없습니다.')
    return upsert(store,'n8n:'+host+':'+credential_id,name='Google Drive · Sheets' if provider=='google' else 'YouTube · 기존 채널',provider=provider,transport='api',role='storage' if provider=='google' else 'publishing',config={'source':'n8n','credential_id':credential_id,'scopes':token.get('scope','').split()},api_key=json.dumps(secret))
