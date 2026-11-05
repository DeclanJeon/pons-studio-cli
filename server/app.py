"""Loopback-only HTTP application. Not a public multi-tenant deployment."""
import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from .store import now,Store, Problem, atomic_json, confined, uid
from .runner import Runner, APP_ROOT
from .providers import CATALOG,voices
from .google import GoogleAuth,archive

MAX_BODY=2*1024*1024
ALLOWED_INPUTS={'.txt','.md','.markdown','.pdf','.png','.jpg','.jpeg','.webp','.mp3','.wav','.m4a','.mp4','.mov'}

class Application:
    def __init__(self,data_dir,port=8765,worker=True):
        self.store=Store(data_dir);self.cookie=secrets.token_urlsafe(32)
        self.instance_lock=None
        if worker:
            import fcntl
            self.instance_lock=(self.store.root/'instance.lock').open('a')
            try:fcntl.flock(self.instance_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                self.instance_lock.close();raise RuntimeError('이 데이터 폴더를 사용하는 제작실이 이미 실행 중입니다.')
        self.base_url=f'http://127.0.0.1:{port}'
        if worker:
            from .bootstrap import seed_skills
            seed_skills(self.store,APP_ROOT)
        self.runner=Runner(self.store,self.base_url) if worker else None
        self.google=GoogleAuth(self.store,self.base_url)
        from .desktop import DesktopEndpoint
        self.desktop=DesktopEndpoint(self)

    def artifact_list(self,pid):
        root=self.store.project_root(pid)
        if not root.exists():return []
        found=[]
        for p in sorted(root.rglob('*')):
            rel=p.relative_to(root)
            if any(x.startswith('.') for x in rel.parts) or p.is_symlink() or not p.is_file():continue
            if p.suffix.lower() not in ('.mp4','.png','.jpg','.jpeg','.webp','.wav','.mp3','.srt','.json','.md','.txt','.pdf'):continue
            if len(found)>=500:break
            found.append({'path':str(rel),'name':p.name,'size':p.stat().st_size,'mime':mimetypes.guess_type(p.name)[0] or 'text/plain'})
        return found

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,addr,app):self.app=app;super().__init__(addr,Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def setup(self):
        super().setup();self.connection.settimeout(30)
    def log_message(self,fmt,*args):pass

    def common_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Frame-Options','DENY')

    def respond(self,data,status=200,headers=None):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status);self.common_headers()
        self.send_header('Content-Type','application/json; charset=utf-8')
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

    def read_json(self):
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise Problem('요청 크기가 올바르지 않습니다.')
        if not 0<length<=MAX_BODY:raise Problem('요청 크기를 확인하세요.',413)
        try:data=json.loads(self.rfile.read(length))
        except (ValueError,UnicodeDecodeError):raise Problem('올바른 JSON 요청이 필요합니다.')
        if not isinstance(data,dict):raise Problem('JSON 객체가 필요합니다.')
        return data

    def check_host(self):
        host=self.headers.get('Host','').split(':')[0]
        if host not in ('127.0.0.1','localhost'):raise Problem('이 앱은 로컬 주소에서만 사용할 수 있습니다.',403)

    def auth(self,write=False):
        cli_token=self.headers.get('X-Pons-Session','')
        if cli_token:
            if self.headers.get('Origin'):raise Problem('CLI 세션은 브라우저에서 사용할 수 없습니다.',403)
            if not self.server.app.desktop.valid_cli_session(cli_token):raise Problem('CLI 연결이 만료됐습니다. 다시 연결하세요.',401)
            return
        c=SimpleCookie()
        try:c.load(self.headers.get('Cookie',''))
        except Exception:raise Problem('제작실을 새로고침하세요.',401)
        if not c.get('studio_session') or not secrets.compare_digest(c['studio_session'].value,self.server.app.cookie):raise Problem('제작실을 새로고침하세요.',401)
        if write:
            if self.headers.get('X-Studio-Request')!='1':raise Problem('허용되지 않은 요청입니다.',403)
            origin=self.headers.get('Origin')
            if origin:
                parsed=urllib.parse.urlsplit(origin)
                if parsed.hostname not in ('127.0.0.1','localhost') or parsed.scheme!='http':raise Problem('허용되지 않은 출처입니다.',403)

    def dispatch(self):
        self.check_host();app=self.server.app;store=app.store
        parsed=urllib.parse.urlsplit(self.path);path=urllib.parse.unquote(parsed.path)
        if path=='/api/cli/session':
            supplied=self.headers.get('X-Pons-Endpoint','')
            if self.command!='POST' or self.headers.get('Origin') or not secrets.compare_digest(supplied,app.desktop.token):raise Problem('CLI 연결 권한이 없습니다.',403)
            self.read_json()
            return self.respond(app.desktop.new_cli_session())
        if path in ('/api/desktop/health','/api/desktop/shutdown'):
            supplied=self.headers.get('X-Pons-Desktop','')
            if not supplied or not secrets.compare_digest(supplied,app.desktop.token):raise Problem('데스크톱 연결 권한이 없습니다.',403)
            if self.command=='GET' and path.endswith('/health'):return self.respond(app.desktop.health())
            if self.command=='POST' and path.endswith('/shutdown'):
                app.desktop.begin_shutdown();self.respond({'ok':True})
                threading.Thread(target=self.server.shutdown,daemon=True).start();return
            raise Problem('허용되지 않은 요청입니다.',405)
        if app.desktop.closing and self.command not in ('GET','HEAD'):raise Problem('제작실이 종료 중입니다. 다시 실행한 뒤 요청하세요.',409)
        if self.command=='GET' and path=='/api/bootstrap':
            origin=self.headers.get('Origin','')
            if origin and urllib.parse.urlsplit(origin).hostname not in ('127.0.0.1','localhost'):raise Problem('허용되지 않은 출처입니다.',403)
            return self.respond({'name':'Pons Studio','version':'0.2.1','workspace_id':app.desktop.identity},headers={'Set-Cookie':f'studio_session={app.cookie}; HttpOnly; SameSite=Strict; Path=/'})
        if self.command=='GET' and path=='/api/oauth/google/callback':
            # The external system browser does not share the webview cookie.
            # GoogleAuth.callback verifies the one-use state, expiry and PKCE.
            q=urllib.parse.parse_qs(parsed.query)
            if q.get('error'):raise Problem('Google 연결을 취소했거나 권한을 받지 못했습니다.')
            app.google.callback(q.get('state',[''])[0],q.get('code',[''])[0])
            raw='<!doctype html><html lang="ko"><meta charset="utf-8"><title>Google 연결 완료</title><body><h1>Google 연결을 완료했습니다.</h1><p>제작실로 돌아가 연결 목록을 새로고침하세요.</p><a href="/">제작실로 돌아가기</a></body></html>'.encode()
            self.send_response(200);self.common_headers();self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        internal=re.fullmatch(r'/api/internal/(prj_[a-f0-9]+)/([a-z]+)',path)
        if internal and self.command=='POST':
            pid,action=internal.groups();expected=app.runner.tokens.get(pid,'')
            supplied=self.headers.get('Authorization','').removeprefix('Bearer ')
            if not expected or not secrets.compare_digest(expected,supplied):raise Problem('작업 권한이 없습니다.',403)
            return self.respond(app.runner.action(pid,action,self.read_json()))
        if path.startswith('/api/'):
            self.auth(write=self.command not in ('GET','HEAD'))
            if self.command=='GET':return self.api_get(path,parsed)
            if self.command=='POST':return self.api_post(path)
            if self.command=='DELETE' and path.startswith('/api/connections/'):
                cid=path.split('/')[-1];store.connection(cid)
                if any(p['status'] in ('queued','running') and cid in p['request'].get('connections',{}).values() for p in store.projects()):raise Problem('이 연결을 사용하는 제작이 있습니다.',409)
                if app.runner.active:raise Problem('제작 종료 후 연결을 해제하세요.',409)
                conn=store.connection(cid)
                if conn['transport']=='mcp':app.runner.codex('mcp','logout',cid)
                with store.db() as db:db.execute('DELETE FROM connections WHERE id=?',(cid,))
                app.runner.config();return self.respond({'ok':True})
            raise Problem('요청 경로를 찾을 수 없습니다.',404)
        if self.command not in ('GET','HEAD'):raise Problem('요청 경로를 찾을 수 없습니다.',404)
        dist=APP_ROOT/'dist'
        file=dist/path.lstrip('/')
        if not file.resolve().is_relative_to(dist.resolve()):raise Problem('허용되지 않은 경로입니다.',403)
        if not file.is_file():file=dist/'index.html'
        if not file.exists():raise Problem('화면 빌드가 필요합니다. npm run build를 실행하세요.',503)
        self.send_file(file,allow_range=False)

    def api_get(self,path,parsed):
        app=self.server.app;store=app.store
        if path=='/api/cli/readiness':
            from dataclasses import asdict
            from .capabilities import authentication
            if app.runner and (app.runner.active or any(p['status']=='queued' for p in store.projects()) or any(s['status']=='pending' for s in app.runner.auth_sessions.values())):return self.respond({'state':'unknown','method':'unknown','installed':bool(shutil.which('codex')),'busy':True})
            return self.respond(asdict(authentication(store)))
        if path=='/api/cli/system-catalog':
            from . import system_integration as system
            try:config=system.system_config(store)
            except Problem:return self.respond([])
            return self.respond([{'name':name,'roles':system.PROVIDERS[name][2],'state':'discovered'} for name in config.get('mcp_servers',{}) if name in system.PROVIDERS])
        if path=='/api/status':return self.respond(app.runner.status())
        if path=='/api/environment':
            from .desktop import environment
            return self.respond(environment(app.runner))
        if path=='/api/catalog':return self.respond(CATALOG)
        if path=='/api/projects':return self.respond(store.progress_views(store.projects()))
        if path=='/api/connections':return self.respond(store.connections())
        match=re.fullmatch(r'/api/connections/(conn_[a-f0-9]+)/voices',path)
        if match:return self.respond(voices(store.connection(match[1],True)))
        match=re.fullmatch(r'/api/auth/(auth_[a-f0-9]+)',path)
        if match:return self.respond(app.runner.auth_state(match[1]))
        match=re.fullmatch(r'/api/projects/(prj_[a-f0-9]+)(?:/(events|event-log|workflow|artifacts|file|messages|production|video|storyboard-md))?',path)
        if match:
            pid,action=match.groups();job=store.project(pid)
            if action=='events':return self.events(pid,parsed)
            if action=='event-log':
                query=urllib.parse.parse_qs(parsed.query)
                try:after=max(0,int(query.get('after',['0'])[0]))
                except ValueError:raise Problem('이벤트 위치가 올바르지 않습니다.')
                return self.respond(store.events(pid,after))
            if action=='workflow':
                from .workflow import Workflow
                return self.respond(Workflow(store,pid).snapshot())
            if action=='artifacts':return self.respond(app.artifact_list(pid))
            if action=='video':
                from .video_runtime import read_quote,public_quote
                return self.respond({'quote':public_quote(read_quote(store,pid))})
            if action=='messages':
                from .conversation import messages
                return self.respond(messages(store,pid))
            if action=='storyboard-md':
                from .storyboard import markdown
                raw=markdown(store.project_root(pid)).encode('utf-8')
                self.send_response(200);self.common_headers();self.send_header('Content-Type','text/markdown; charset=utf-8');self.send_header('Content-Disposition','attachment; filename="storyboard_design.md"');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if action=='production':
                from .production import inspect
                return self.respond(inspect(store.project_root(pid)))
            if action=='file':
                q=urllib.parse.parse_qs(parsed.query);rel=q.get('path',[''])[0]
                if any(part.startswith('.') for part in Path(rel).parts):raise Problem('비공개 실행 파일입니다.',403)
                file=confined(store.project_root(pid),rel)
                return self.send_file(file,download='download' in q)
            return self.respond(store.progress_views([job])[0])
        raise Problem('요청 경로를 찾을 수 없습니다.',404)

    def api_post(self,path):
        app=self.server.app;store=app.store
        if path=='/api/inputs':
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:raise Problem('파일 크기를 확인하세요.')
            if not 0<length<=100*1024*1024:raise Problem('파일은 100 MB 이하로 올려주세요.',413)
            name=Path(urllib.parse.unquote(self.headers.get('X-File-Name','file'))).name
            name=re.sub(r'[^\w.가-힣 ()-]','_',name)[:180]
            if Path(name).suffix.lower() not in ALLOWED_INPUTS:raise Problem('지원하지 않는 파일 형식입니다.')
            iid=uid('input');file=store.root/'inputs'/iid
            digest=hashlib.sha256();remaining=length
            try:
                with file.open('xb') as f:
                    while remaining:
                        chunk=self.rfile.read(min(1024*1024,remaining))
                        if not chunk:raise Problem('파일 전송이 중단됐습니다.')
                        f.write(chunk);digest.update(chunk);remaining-=len(chunk)
                mime=mimetypes.guess_type(name)[0] or 'application/octet-stream'
                with store.db() as db:db.execute('INSERT INTO inputs VALUES(?,?,?,?,?,?)',(iid,name,str(file),length,digest.hexdigest(),mime))
            except Exception:file.unlink(missing_ok=True);raise
            return self.respond({'id':iid,'name':name,'size':length,'mime':mime},201)
        data=self.read_json()
        if path=='/api/cli/connect-system':
            if app.runner.active or any(p['status']=='queued' for p in store.projects()) or any(s['status']=='pending' for s in app.runner.auth_sessions.values()):raise Problem('제작/인증 종료 후 도구를 연결하세요.',409)
            from .capabilities import select_system_connections
            names=data.get('names')
            if not isinstance(names,list) or any(not isinstance(n,str) for n in names):raise Problem('MCP 이름 목록을 입력하세요.')
            result=select_system_connections(store,names);app.runner.config()
            return self.respond(result)
        if path=='/api/environment/skill':
            if app.runner.active or any(p['status'] in ('queued','running','archiving') for p in store.projects()):raise Problem('제작이 끝난 뒤 스킬을 연결하세요.',409)
            name=data.get('name');value=data.get('path')
            if name not in ('codex-imagen','ai-delegation-loop') or not isinstance(value,str):raise Problem('연결할 스킬을 확인하세요.')
            file=Path(value).expanduser().resolve()
            if file.is_dir():file=file/'SKILL.md'
            if file.name!='SKILL.md' or not file.is_file() or file.stat().st_size>1024*1024:raise Problem('실제 스킬 폴더의 SKILL.md를 선택하세요.')
            if not re.search(r'^name:\s*[\"\']?'+re.escape(name)+r'[\"\']?\s*$',file.read_text(),re.M):raise Problem('선택한 파일의 스킬 이름이 다릅니다.')
            from .system_integration import runtime
            settings=runtime(store);settings['additional_skill_paths']=list(dict.fromkeys([str(file),*settings.get('additional_skill_paths',[])]))
            atomic_json(store.root/'runtime.json',settings);return self.respond({'ok':True,'name':name})
        if path=='/api/agent/projects':
            from .conversation import new_request
            request=new_request(store,data);app.runner.preflight(request)
            key=self.headers.get('Idempotency-Key')
            if not key or len(key)>200:raise Problem('대화 요청 식별자가 필요합니다.')
            return self.respond(store.create(request,key),202)
        match=re.fullmatch(r'/api/projects/(prj_[a-f0-9]+)/agent',path)
        if match:
            if match[1] in getattr(app.runner,'active',{}):raise Problem('현재 작업을 마친 뒤 대화로 연결하세요.',409)
            from .conversation import attach_existing
            return self.respond(attach_existing(store,match[1]))
        match=re.fullmatch(r'/api/projects/(prj_[a-f0-9]+)/messages',path)
        if match:
            from .conversation import post_message
            pid=match[1];job=store.project(pid)
            if not isinstance(data.get('inputs',[]),list) or not isinstance(data.get('reference_urls',[]),list):raise Problem('첨부 자료를 확인하세요.')
            if any(not isinstance(x,str) for x in [*data.get('inputs',[]),*data.get('reference_urls',[])]):raise Problem('첨부 자료 ID와 URL은 문자열이어야 합니다.')
            # Validate new attachments/URLs with the same public-source and size rules.
            candidate={**job['request'],'inputs':list(dict.fromkeys(job['request'].get('inputs',[])+data.get('inputs',[]))),
                       'reference_urls':list(dict.fromkeys(job['request'].get('reference_urls',[])+data.get('reference_urls',[])))}
            app.runner.preflight(candidate)
            return self.respond(post_message(store,pid,data,defer_active=pid in getattr(app.runner,'active',{})),202)
        match=re.fullmatch(r'/api/projects/(prj_[a-f0-9]+)/(video-approval|video-settings)',path)
        if match:
            pid,operation=match.groups();job=store.project(pid)
            if operation=='video-approval':
                from .video_runtime import approve
                return self.respond(approve(store,pid,data.get('quote_id','')),202)
            if pid in getattr(app.runner,'active',{}) or job['status'] in ('queued','running','archiving','cancelling'):raise Problem('현재 작업을 마친 뒤 영상 도구를 바꾸세요.',409)
            cid=data.get('connection_id');motion=data.get('motion_video_authorized') is True
            if cid and not motion:raise Problem('이미지 애니매틱에는 영상 도구를 선택하지 않습니다.')
            connection=store.connection(cid) if cid else None
            if connection and (connection['role']!='video' or connection['status'] not in ('verified','authenticated','ready')):raise Problem('준비된 영상 도구를 선택하세요.')
            request={**job['request'],'motion_video_authorized':motion,'delivery_kind':'video_edit' if motion else 'animatic','connections':{**job['request'].get('connections',{}),'video':cid if motion else None}}
            if not motion:
                from .video_runtime import cancel_pending
                cancel_pending(store,pid,'사용자가 이미지 애니매틱 제작임을 명확히 했습니다.')
            with store.db() as db:db.execute('UPDATE projects SET request=?,updated_at=? WHERE id=?',(json.dumps(request,ensure_ascii=False),now(),pid))
            store.event(pid,'state','영상 도구 선택을 반영했습니다. 생성 실행은 새 지시와 견적 승인을 따릅니다.')
            return self.respond(store.project(pid))
        if path=='/api/system/import':
            if app.runner.active or any(p['status']=='queued' for p in store.projects()):raise Problem('제작 종료 후 시스템 설정을 가져오세요.',409)
            if any(s['status']=='pending' for s in app.runner.auth_sessions.values()):raise Problem('진행 중인 인증을 먼저 마치세요.',409)
            from .system_integration import import_system
            report=import_system(store);app.runner.config();return self.respond(report)
        if path=='/api/projects':
            app.runner.preflight(data)
            key=self.headers.get('Idempotency-Key')
            if not key or len(key)>200:raise Problem('제작 요청 키가 필요합니다.')
            return self.respond(store.create(data,key),202)
        if path=='/api/connections':
            if app.runner.active:raise Problem('제작 종료 후 연결을 변경하세요.',409)
            self.validate_connection(data)
            result=store.save_connection(data);app.runner.config();return self.respond(result,201)
        if path=='/api/auth/codex':return self.respond(app.runner.start_auth(),202)
        if path=='/api/auth/google':return self.respond(app.google.start(data.get('client_id',''),data.get('client_secret','')),202)
        match=re.fullmatch(r'/api/auth/(auth_[a-f0-9]+)/(reply|cancel)',path)
        if match:
            sid,action=match.groups()
            if action=='reply':return self.respond(app.runner.auth_reply(sid,data.get('callback','')))
            app.runner.cancel_auth(sid);return self.respond({'ok':True})
        match=re.fullmatch(r'/api/connections/(conn_[a-f0-9]+)/(auth|verify)',path)
        if match:
            cid,action=match.groups();conn=store.connection(cid,True)
            if action=='auth':return self.respond(app.runner.start_auth(cid),202)
            if conn['transport']=='api':
                if conn['provider'] in ('google','youtube'):
                    from .google import verify_connection
                    verify_connection(store,cid)
                else:voices(conn)
                store.set_connection_status(cid,'verified')
            elif conn['transport']=='system_mcp':
                from .system_integration import system_config
                name=conn['config']['system_mcp_name']
                if name not in system_config(store).get('mcp_servers',{}):raise Problem('시스템 MCP 설정이 삭제됐습니다.')
                if conn['status']!='verified':store.set_connection_status(cid,'ready')
            elif conn['transport']=='cli':
                command=conn['config']['command']
                if not shutil.which(command[0]):raise Problem('CLI 실행 파일을 찾을 수 없습니다.')
                store.set_connection_status(cid,'ready')
            else:raise Problem('MCP는 계정 연결을 통해 인증하세요.')
            return self.respond(store.connection(cid))
        match=re.fullmatch(r'/api/projects/(prj_[a-f0-9]+)/(cancel|resume|archive|revise)',path)
        if match:
            pid,action=match.groups();job=store.project(pid)
            if action=='cancel':
                if job['status'] in ('queued','running'):
                    store.update(pid,status='cancelling' if job['status']=='running' else 'cancelled')
                    store.event(pid,'state','제작 중지를 요청했습니다.')
                return self.respond(store.project(pid))
            if action=='resume':
                if job['status'] not in ('failed','interrupted','cancelled'):raise Problem('지금은 이어서 진행할 수 없습니다.',409)
                if job['request'].get('workflow_enabled'):
                    if data.get('remote_checked') is not True:raise Problem('원격 작업 상태를 확인한 뒤 재개하세요.',409)
                    from .workflow import Workflow
                    Workflow(store,pid).reconcile()
                app.runner.preflight(job['request']);store.update(pid,status='queued',error=None,started_at=None,repair_count=0)
                store.event(pid,'state','기존 결과와 공급자 작업을 확인하고 이어서 진행합니다.')
                return self.respond(store.project(pid))
            if action=='revise':
                if job['status'] not in ('animatic_ready','completed','failed'):raise Problem('제작이 멈춘 상태에서 수정 요청을 보내세요.',409)
                revision=data.get('prompt','').strip()
                if not revision or len(revision)>10000:raise Problem('수정할 내용을 입력하세요.')
                if data.get('base_attempt')!=job['attempt']:raise Problem('제작 상태가 변경됐습니다. 새로고침 후 다시 요청하세요.',409)
                root=store.project_root(pid);old=job['request'];old.setdefault('revisions',[]).append({'prompt':revision,'at':time.time()})
                if data.get('require_production_handoff') is True:old['production_contract_version']=2
                app.runner.preflight(old)
                with store.db() as db:db.execute('UPDATE projects SET request=? WHERE id=?',(json.dumps(old,ensure_ascii=False),pid))
                store.update(pid,status='queued',error=None,started_at=None,repair_count=0);store.event(pid,'state','수정 요청을 접수했습니다. 관련 에셋만 갱신합니다.')
                return self.respond(store.project(pid))
            if action=='archive':
                if job['status'] not in ('animatic_ready','archive_failed'):raise Problem('보관할 완성 애니매틱이 없습니다.',409)
                conn=next((c for c in store.connections() if c['provider']=='google' and c['status']=='verified'),None)
                if not conn:raise Problem('Google 계정을 연결하세요.')
                if not store.transition(pid,['animatic_ready','archive_failed'],'archiving','archive'):raise Problem('보관 작업이 이미 진행 중입니다.',409)
                def run():
                    try:
                        result=job['result'];result['archive']=archive(store,conn['id'],pid,store.project_root(pid),result)
                        store.update(pid,status='completed',result=result,error=None);store.event(pid,'state','Google 보관을 완료했습니다.')
                    except Exception:store.update(pid,status='archive_failed',error='Google 보관에 실패했습니다. 결과를 보존했습니다.')
                threading.Thread(target=run,daemon=True).start();return self.respond(store.project(pid),202)
        raise Problem('요청 경로를 찾을 수 없습니다.',404)

    def validate_connection(self,data):
        provider=data.get('provider');transport=data.get('transport');role=data.get('role');cfg=data.get('config',{})
        if not isinstance(cfg,dict):raise Problem('연결 설정을 확인하세요.')
        cat=next((p for p in CATALOG if p['id']==provider),None)
        if not cat or transport not in cat['transports'] or role not in cat['roles']:raise Problem('공급자와 연결 방식을 확인하세요.')
        if not isinstance(data.get('name'),str) or not 1<=len(data['name'])<=80:raise Problem('연결 이름을 입력하세요.')
        if transport=='api':
            if role!='speech':raise Problem('API 연결은 현재 음성 기능만 지원합니다. 이미지는 MCP 또는 CLI를 사용하세요.')
            if not isinstance(data.get('api_key'),str) or not 8<=len(data['api_key'])<=1000:raise Problem('API 키를 입력하세요.')
            data['config']={}
        if transport=='mcp':
            parsed=urllib.parse.urlsplit(cfg.get('url',''))
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise Problem('인증 정보가 없는 HTTPS MCP 주소를 입력하세요.')
            import socket,ipaddress
            try:addresses=socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM)
            except OSError:raise Problem('MCP 서버 주소를 확인할 수 없습니다.')
            if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise Problem('공개 HTTPS MCP 서버만 등록할 수 있습니다.')
            if provider!='custom' and cfg.get('url')!=cat.get('mcp_url'):raise Problem('이 공급자의 공식 MCP 주소를 사용하세요.')
            data['config']={'url':cfg['url']}
        if transport=='cli':
            command=cfg.get('command')
            if not isinstance(command,list) or not 1<=len(command)<=20 or any(not isinstance(x,str) or len(x)>500 for x in command):raise Problem('실행 파일과 인자를 JSON 배열로 입력하세요.')
            if not shutil.which(command[0]):raise Problem('이 컴퓨터에서 CLI 실행 파일을 찾을 수 없습니다.')
            if any(x in ('sh','bash','zsh','cmd','powershell') for x in [Path(command[0]).name]):raise Problem('셸 대신 실제 제작 도구의 실행 파일을 등록하세요.')
            data['config']={'command':command,**({'runtime':'higgsfield'} if role=='video' and Path(command[0]).name in ('higgsfield','hf') else {})}

    def events(self,pid,parsed):
        q=urllib.parse.parse_qs(parsed.query)
        try:after=max(0,int(self.headers.get('Last-Event-ID') or q.get('after',['0'])[0]))
        except ValueError:raise Problem('이벤트 위치가 올바르지 않습니다.')
        self.send_response(200);self.common_headers();self.send_header('Content-Type','text/event-stream; charset=utf-8');self.send_header('Connection','close');self.end_headers()
        self.wfile.write(b'retry: 500\n\n');self.wfile.flush()
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            rows=self.server.app.store.events(pid,after)
            for event in rows:
                self.wfile.write(f'id: {event["seq"]}\ndata: {json.dumps(event,ensure_ascii=False)}\n\n'.encode());after=event['seq']
            self.wfile.write(b': heartbeat\n\n');self.wfile.flush();time.sleep(1)
        self.close_connection=True

    def send_file(self,file,download=False,allow_range=True):
        size=file.stat().st_size;start=0;end=size-1;status=200
        if allow_range and self.headers.get('Range'):
            m=re.fullmatch(r'bytes=(\d*)-(\d*)',self.headers['Range'])
            if not m or not any(m.groups()):raise Problem('범위 요청이 올바르지 않습니다.',416)
            if m[1]:start=int(m[1]);end=min(int(m[2]) if m[2] else end,end)
            else:start=max(0,size-int(m[2]))
            if start>end or start>=size:raise Problem('요청한 파일 범위를 찾을 수 없습니다.',416)
            status=206
        mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        self.send_response(status);self.common_headers();self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(max(0,end-start+1)));self.send_header('Accept-Ranges','bytes')
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        if download:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(file.name))
        if not allow_range and mime=='text/html':self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'")
        elif mime in ('image/svg+xml','text/html'):self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'none'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command=='HEAD':return
        with file.open('rb') as f:
            f.seek(start);left=end-start+1
            while left>0:
                chunk=f.read(min(left,256*1024))
                if not chunk:break
                self.wfile.write(chunk);left-=len(chunk)

    def do_request(self):
        try:self.dispatch()
        except (BrokenPipeError,ConnectionResetError):pass
        except Problem as e:
            try:self.respond({'error':e.message},e.status)
            except (BrokenPipeError,ConnectionResetError):pass
        except (KeyError,TypeError,ValueError):self.respond({'error':'입력 형식을 확인하세요.'},400)
        except Exception:self.respond({'error':'요청을 처리하지 못했습니다. 저장된 작업은 보존됩니다.'},500)
    do_GET=do_POST=do_DELETE=do_HEAD=do_request

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765);parser.add_argument('--data',default=os.environ.get('STUDIO_DATA',str(APP_ROOT/'.studio')));args=parser.parse_args()
    app=Application(args.data,args.port)
    # Application seeds shipped instructions before its worker can claim queued work.
    server=Server(('127.0.0.1',args.port),app)
    app.base_url=f'http://127.0.0.1:{server.server_port}'
    app.runner.base_url=app.base_url;app.google.base_url=app.base_url
    app.desktop.publish()
    def terminate(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,terminate)
    print(f'Pons Studio: {app.base_url}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        app.runner.stop.set()
        for proc in list(app.runner.active.values()):app.runner.kill(proc)
        for session in list(app.runner.auth_sessions.values()):
            if session.get('_proc') is not None:app.runner.kill(session['_proc'])
        app.runner.worker.join(timeout=5)
        server.server_close()
        app.desktop.close()
        if app.instance_lock:app.instance_lock.close()

if __name__=='__main__':main()
