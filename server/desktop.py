"""Private same-user desktop discovery. Never publish credentials to the UI."""
import hashlib
import os
import secrets
import threading
import time
from pathlib import Path
from .store import atomic_json

BUSY={'queued','running','archiving','cancelling'}

class DesktopEndpoint:
    def __init__(self,app):
        self.app=app
        self.token=secrets.token_urlsafe(32)
        self.path=app.store.root/'desktop-endpoint.json'
        self.identity=hashlib.sha256(str(app.store.root).encode()).hexdigest()
        self.closing=False
        self.cli_sessions={}
        self.session_lock=threading.Lock()

    def new_cli_session(self):
        with self.session_lock:
            self.cli_sessions={k:v for k,v in self.cli_sessions.items() if v>time.monotonic()}
            if len(self.cli_sessions)>=128:
                from .store import Problem
                raise Problem('CLI 연결이 너무 많습니다. 기존 연결 만료 후 다시 시도하세요.',429)
            token=secrets.token_urlsafe(32)
            self.cli_sessions[token]=time.monotonic()+8*3600
            return {'token':token,'expires_in':8*3600,'data_id':self.identity,'protocol':1}

    def valid_cli_session(self,token):
        with self.session_lock:
            expiry=self.cli_sessions.get(token,0)
            return bool(token) and expiry>time.monotonic()

    def publish(self):
        atomic_json(self.path,{'url':self.app.base_url,'token':self.token,'data_id':self.identity,'pid':os.getpid(),'protocol':1})
        self.path.chmod(0o600)

    def health(self):
        return {'name':'Pons Studio','protocol':1,'cli_protocol':1,'data_id':self.identity,
                'busy':any(p['status'] in BUSY for p in self.app.store.projects()),
                'transport':transport(self.app.store)}

    def begin_shutdown(self):
        from .store import Problem
        with self.app.store.lock:
            if self.health()['busy'] or any(s.get('status')=='pending' for s in getattr(self.app.runner,'auth_sessions',{}).values()):
                raise Problem('제작 또는 인증이 진행 중입니다. 작업을 마친 뒤 종료하세요.',409)
            self.closing=True
            self.app.store.closing=True

    def close(self):
        import json
        try:
            if json.loads(self.path.read_text()).get('token')==self.token:self.path.unlink()
        except (OSError,ValueError):pass

def transport(store):
    from .system_integration import runtime
    value=os.environ.get('STUDIO_CODEX_TRANSPORT') or runtime(store).get('codex_transport','exec')
    return value if value in ('exec','app-server') else 'exec'

def environment(runner):
    import shutil
    from . import system_integration as system
    check=runner.codex('login','status')
    details=((check.stdout or '')+' '+(check.stderr or '')).lower() if check else ''
    method='chatgpt' if check and check.returncode==0 and 'chatgpt' in details else 'api_key' if check and check.returncode==0 and ('api key' in details or 'api_key' in details) else 'unknown'
    return {'transport':transport(runner.store),'authentication':method,
            'tools':[{'name':n,'installed':bool(shutil.which(n))} for n in ('codex','node','uv','uvx','ffmpeg','ffprobe','higgsfield')],
            'skills':{'codex-imagen':bool(system.imagen_skill(runner.store)),
                      'ai-delegation-loop':bool(system.coordination_skill(runner.store)),
                      'creative-production':(runner.store.root/'skills/source/creative-production/SKILL.md').is_file()},
            'connections':[{'id':c['id'],'name':c['name'],'transport':c['transport'],'status':c['status'],
                            'billing':'provider_account' if c['transport'] in ('mcp','system_mcp','cli') else 'api_usage'} for c in runner.store.connections()],
            'billing_note':'구독 포함 여부와 잔액은 공급자별로 확인하세요. MCP·CLI 연결이 무료 사용을 의미하지 않습니다.'}
