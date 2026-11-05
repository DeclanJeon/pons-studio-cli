"""Operator-owned Google OAuth, resumable Drive archive, and Sheets projection."""
import base64
import hashlib
import json
import mimetypes
import secrets
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from .store import Problem, atomic_json, now

def call(url,token=None,body=None,method=None,headers=None,accepted=()):
    h=dict(headers or {})
    if token:h['Authorization']='Bearer '+token
    if isinstance(body,dict):body=json.dumps(body).encode();h['Content-Type']='application/json'
    req=urllib.request.Request(url,data=body,headers=h,method=method)
    try:
        with urllib.request.urlopen(req,timeout=90) as r:return r.status,dict(r.headers),r.read()
    except urllib.error.HTTPError as e:
        if e.code in accepted:return e.code,dict(e.headers),e.read()
        raise Problem(f'Google 요청에 실패했습니다. 권한·저장 용량·설정을 확인하세요. HTTP {e.code}',502)
    except (urllib.error.URLError,TimeoutError):raise Problem('Google 연결이 지연되고 있습니다. 저장된 결과를 보존했습니다.',502)

def jsoncall(url,token=None,body=None,method=None):return json.loads(call(url,token,body,method)[2])

class GoogleAuth:
    def __init__(self,store,base_url):self.store,self.base_url,self.pending=store,base_url,{}

    def start(self,client_id,client_secret):
        if not client_id.endswith('.apps.googleusercontent.com') or not client_secret:raise Problem('Google 웹 애플리케이션 OAuth 클라이언트 정보를 입력하세요.')
        state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        redirect=self.base_url+'/api/oauth/google/callback'
        self.pending[state]={'client_id':client_id,'client_secret':client_secret,'verifier':verifier,'expires':time.time()+600,'redirect':redirect}
        args={'client_id':client_id,'redirect_uri':redirect,'response_type':'code','scope':'https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/spreadsheets','state':state,'access_type':'offline','prompt':'consent','code_challenge':challenge,'code_challenge_method':'S256'}
        return {'url':'https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode(args),'redirect_uri':redirect}

    def callback(self,state,code):
        p=self.pending.pop(state,None)
        if not p or p['expires']<time.time():raise Problem('Google 인증 요청이 만료됐거나 일치하지 않습니다.')
        fields={'client_id':p['client_id'],'client_secret':p['client_secret'],'code_verifier':p['verifier'],'redirect_uri':p['redirect'],'code':code,'grant_type':'authorization_code'}
        _,_,raw=call('https://oauth2.googleapis.com/token',body=urllib.parse.urlencode(fields).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})
        token=json.loads(raw)
        if not token.get('refresh_token'):raise Problem('지속적인 보관을 위한 Google 권한을 받지 못했습니다. 동의 화면을 다시 확인하세요.')
        secret=json.dumps({'client_id':p['client_id'],'client_secret':p['client_secret'],'refresh_token':token['refresh_token']})
        conn=self.store.save_connection({'name':'Google Drive · Sheets','provider':'google','transport':'api','role':'storage','config':{},'api_key':secret})
        self.store.set_connection_status(conn['id'],'verified')
        return conn

def access_token(store,cid):
    c=store.connection(cid,secret=True);s=json.loads(c['secret'])
    body=urllib.parse.urlencode({k:s[k] for k in ('client_id','client_secret','refresh_token')}|{'grant_type':'refresh_token'}).encode()
    _,_,raw=call('https://oauth2.googleapis.com/token',body=body,headers={'Content-Type':'application/x-www-form-urlencoded'})
    token=json.loads(raw)
    if token.get('refresh_token') and token['refresh_token']!=s['refresh_token']:
        s['refresh_token']=token['refresh_token']
        with store.db() as db:db.execute('UPDATE connections SET secret=? WHERE id=?',(store.vault.encrypt(json.dumps(s).encode()),cid))
    return token['access_token']

def verify_connection(store,cid):
    conn=store.connection(cid);token=access_token(store,cid)
    if conn['provider']=='youtube':
        result=jsoncall('https://www.googleapis.com/youtube/v3/channels?part=id&mine=true',token)
        if not result.get('items'):raise Problem('연결된 Google 계정의 YouTube 채널을 찾지 못했습니다.')
        capabilities={'channel_read':True,'publication_enabled':False}
    else:
        jsoncall('https://www.googleapis.com/drive/v3/about?fields=storageQuota',token)
        # A deliberately absent ID probes Sheets API access without creating a file.
        status,_,_=call('https://sheets.googleapis.com/v4/spreadsheets/pons-studio-capability-probe?fields=spreadsheetId',token,accepted=(404,))
        capabilities={'drive':True,'sheets':status in (200,404)}
    cfg={**conn['config'],'capabilities':capabilities}
    with store.db() as db:db.execute('UPDATE connections SET config=? WHERE id=?',(json.dumps(cfg),cid))
    return capabilities

def allocate(token):return jsoncall('https://www.googleapis.com/drive/v3/files/generateIds?count=1&space=drive',token)['ids'][0]

def create_folder(token,file_id,name,parent=None):
    body={'id':file_id,'name':name,'mimeType':'application/vnd.google-apps.folder'}
    if parent:body['parents']=[parent]
    call('https://www.googleapis.com/drive/v3/files',token,body,accepted=(409,))

def upload(token,path,file_id,parent,state,checkpoint):
    size=path.stat().st_size
    digest=hashlib.md5(path.read_bytes()).hexdigest()
    status,_,raw=call(f'https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,size,md5Checksum',token,accepted=(404,))
    if status==200:
        remote=json.loads(raw)
        if remote.get('md5Checksum')==digest and int(remote.get('size',-1))==size:return
        raise Problem('이미 보관된 파일의 검증 값이 다릅니다. 자동 덮어쓰기를 중지합니다.')
    session=state.get('session');offset=0
    if session:
        status,headers,_=call(session,token,b'',method='PUT',headers={'Content-Range':f'bytes */{size}'},accepted=(308,404,410))
        if status in (200,201):return upload(token,path,file_id,parent,{},checkpoint)
        if status in (404,410):session=None
        elif headers.get('Range'):offset=int(headers['Range'].split('-')[-1])+1
    if not session:
        metadata={'id':file_id,'name':path.name,'parents':[parent]}
        _,headers,_=call('https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable',token,metadata,headers={'X-Upload-Content-Length':str(size),'X-Upload-Content-Type':mimetypes.guess_type(path.name)[0] or 'application/octet-stream'})
        session=headers.get('Location') or headers.get('location')
        if not session or not session.startswith('https://www.googleapis.com/'):raise Problem('Google 업로드 세션을 확인할 수 없습니다.')
        state['session']=session;checkpoint()
    with path.open('rb') as f:
        f.seek(offset)
        while offset<size:
            chunk=f.read(8*1024*1024)
            call(session,token,chunk,method='PUT',headers={'Content-Range':f'bytes {offset}-{offset+len(chunk)-1}/{size}','Content-Type':'application/octet-stream'},accepted=(308,))
            offset+=len(chunk)
    remote=jsoncall(f'https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,size,md5Checksum',token)
    if remote.get('md5Checksum')!=digest or int(remote.get('size',-1))!=size:raise Problem('Drive 파일 검증에 실패했습니다.')
    state.pop('session',None);state['verified']=True;checkpoint()

def archive(store,cid,pid,root,result):
    token=access_token(store,cid)
    state_path=store.root/'archives'/f'{pid}.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'folders':{},'files':{}}
    def checkpoint():atomic_json(state_path,state)
    if 'project' not in state['folders']:state['folders']['project']=allocate(token);checkpoint()
    parent=state['folders']['project'];create_folder(token,parent,pid)
    ensured_folders=set()
    for path in sorted(Path(root).rglob('*')):
        if not path.is_file() or path.is_symlink():continue
        rel=path.relative_to(root)
        if any(x.startswith('.') and x!='.history' for x in rel.parts):continue
        if path.suffix in ('.tmp','.lock') or path.stat().st_size==0:continue
        # Assets and canonical history only; private Codex sessions are outside the tree.
        folder=parent
        for depth in range(1,len(rel.parts)):
            sub=str(Path(*rel.parts[:depth]))
            if sub not in state['folders']:state['folders'][sub]=allocate(token);checkpoint()
            if sub not in ensured_folders:
                create_folder(token,state['folders'][sub],rel.parts[depth-1],folder)
                ensured_folders.add(sub)
            folder=state['folders'][sub]
        fingerprint=hashlib.sha256(path.read_bytes()).hexdigest();key=str(rel)+'@'+fingerprint
        if key not in state['files']:state['files'][key]={'id':allocate(token),'sha256':fingerprint};checkpoint()
        item=state['files'][key];upload(token,path,item['id'],folder,item,checkpoint)
    if not state.get('sheet_id'):
        sheet=jsoncall('https://sheets.googleapis.com/v4/spreadsheets',token,{'properties':{'title':f'Pons Studio {pid}'},'sheets':[{'properties':{'title':'Projects'}}]})
        state['sheet_id']=sheet['spreadsheetId'];checkpoint()
    # Stable range update is idempotent; retrying never appends duplicate rows.
    rows=[['project_id','status','duration_s','drive_folder','updated_at'],[pid,'archived',str(result['duration_s']),parent,now()]]
    jsoncall(f'https://sheets.googleapis.com/v4/spreadsheets/{state["sheet_id"]}/values/Projects!A1:E2?valueInputOption=RAW',token,{'range':'Projects!A1:E2','majorDimension':'ROWS','values':rows},'PUT')
    state['completed_at']=now();checkpoint()
    return {'drive_url':f'https://drive.google.com/drive/folders/{parent}','sheets_url':f'https://docs.google.com/spreadsheets/d/{state["sheet_id"]}','file_count':len(state['files'])}
