"""Persistent operator-local job store. Content authority stays in project.json."""
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from cryptography.fernet import Fernet

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return f'{prefix}_{uuid.uuid4().hex[:16]}'

class Problem(Exception):
    def __init__(self, message, status=400): self.message, self.status = message, status

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for name in ('projects', 'inputs', 'codex', 'skills'):
            (self.root/name).mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.closing = False
        key = self.root/'vault.key'
        try:
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError: pass
        else:
            with os.fdopen(fd, 'wb') as f: f.write(Fernet.generate_key())
        self.vault = Fernet(key.read_bytes())
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
              stage TEXT NOT NULL, request TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, error TEXT, session_id TEXT,
              attempt INTEGER NOT NULL DEFAULT 0, result TEXT, idempotency TEXT UNIQUE,
              payload_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
              kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inputs (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
              size INTEGER NOT NULL, sha256 TEXT NOT NULL, mime TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS connections (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
              transport TEXT NOT NULL, role TEXT NOT NULL, config TEXT NOT NULL,
              secret BLOB, status TEXT NOT NULL, checked_at TEXT);
            CREATE TABLE IF NOT EXISTS calls (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL,
              status TEXT NOT NULL, result TEXT, characters INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS chat_messages (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, client_id TEXT NOT NULL,
              content TEXT NOT NULL, meta TEXT NOT NULL, fingerprint TEXT NOT NULL,
              at TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER,
              UNIQUE(project_id,client_id));
            CREATE INDEX IF NOT EXISTS chat_project_state ON chat_messages(project_id,state,at);
            ''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(projects)')}
            if 'started_at' not in columns:db.execute('ALTER TABLE projects ADD COLUMN started_at TEXT')
            if 'repair_count' not in columns:db.execute('ALTER TABLE projects ADD COLUMN repair_count INTEGER NOT NULL DEFAULT 0')
            if 'agent_outcome' not in columns:db.execute('ALTER TABLE projects ADD COLUMN agent_outcome TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS events_project_seq ON events(project_id,seq)')
            db.execute('CREATE TABLE IF NOT EXISTS project_locations (project_id TEXT PRIMARY KEY, workspace TEXT NOT NULL)')
            from .workflow import migrate
            migrate(db)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.root/'studio.sqlite', timeout=20)) as db:
            db.row_factory = sqlite3.Row
            with db:yield db

    def projects(self):
        with self.db() as db: rows=db.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
        return [self.unpack(row) for row in rows]

    def unpack(self, row):
        obj=dict(row)
        for k in ('request','result'):
            if k in obj: obj[k]=json.loads(obj[k]) if obj[k] else None
        obj.pop('idempotency',None);obj.pop('payload_hash',None)
        return obj

    def project(self, project_id):
        with self.db() as db: row=db.execute('SELECT * FROM projects WHERE id=?',(project_id,)).fetchone()
        if not row: raise Problem('제작 요청을 찾을 수 없습니다.',404)
        return self.unpack(row)

    def progress_views(self, jobs):
        from .progress import snapshot
        history = self.projects()
        with self.db() as db:
            views = []
            for job in jobs:
                latest = db.execute('SELECT at,message FROM events WHERE project_id=? ORDER BY seq DESC LIMIT 1', (job['id'],)).fetchone()
                views.append({**job, 'progress': snapshot(job, dict(latest) if latest else None, history)})
        return views

    def create(self, request, key):
        raw=json.dumps(request,ensure_ascii=False,sort_keys=True)
        fingerprint=hashlib.sha256(raw.encode()).hexdigest()
        with self.lock, self.db() as db:
            if self.closing:raise Problem('제작실이 종료 중입니다. 다시 실행한 뒤 요청하세요.',409)
            old=db.execute('SELECT * FROM projects WHERE idempotency=?',(key,)).fetchone()
            if old:
                if old['payload_hash']!=fingerprint: raise Problem('같은 요청 키에 다른 내용이 전달됐습니다.',409)
                return self.unpack(old)
            project_id=uid('prj');at=now()
            workspace=request.get('workspace')
            if workspace is not None:
                from .workspaces import workspace_directory
                if not isinstance(workspace,str) or not workspace.strip():raise Problem('작업 폴더를 확인하세요.')
                folder=workspace_directory(workspace,self.root)
                target=folder/'pons-projects'/project_id
                target.mkdir(parents=True,mode=0o700)
                db.execute('INSERT INTO project_locations VALUES(?,?)',(project_id,str(folder)))
            db.execute('INSERT INTO projects(id,title,status,stage,request,created_at,updated_at,idempotency,payload_hash) VALUES(?,?,?,?,?,?,?,?,?)',
              (project_id,request.get('title') or request['user_prompt'][:42], 'queued','intake',raw,at,at,key,fingerprint))
            if request.get('workflow_enabled'):
                from .workflow import initialize
                initialize(db,project_id,request.get('recipe','narrated_stills'))
            if request.get('interaction_mode')=='agent':
                meta={'inputs':request.get('inputs',[]),'reference_urls':request.get('reference_urls',[]),'execution_authorized':request.get('execution_authorized') is True}
                db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?)',(uid('msg'),project_id,key,request['user_prompt'],json.dumps(meta,ensure_ascii=False),fingerprint,at,'pending',None))
        self.event(project_id,'state','제작 요청을 접수했습니다.',{'stage':'intake'})
        return self.project(project_id)

    def update(self, project_id, **values):
        allowed={'status','stage','error','session_id','attempt','result','started_at','repair_count','agent_outcome'}
        if not set(values)<=allowed: raise ValueError('invalid update')
        values['updated_at']=now()
        if 'result' in values: values['result']=json.dumps(values['result'],ensure_ascii=False)
        with self.db() as db:
            db.execute('UPDATE projects SET '+','.join(f'{k}=?' for k in values)+' WHERE id=?',(*values.values(),project_id))

    def event(self, project_id, kind, message, data=None):
        with self.db() as db:
            db.execute('INSERT INTO events(project_id,kind,message,data,at) VALUES(?,?,?,?,?)',
                       (project_id,kind,message[:4000],json.dumps(data or {},ensure_ascii=False),now()))

    def transition(self,project_id,from_states,to_state,stage=None):
        with self.db() as db:
            placeholders=','.join('?' for _ in from_states)
            query=f'UPDATE projects SET status=?,updated_at=?'+(',stage=?' if stage else '')+f' WHERE id=? AND status IN ({placeholders})'
            args=[to_state,now()]+([stage] if stage else [])+[project_id,*from_states]
            return db.execute(query,args).rowcount==1

    def events(self, project_id, after=0):
        self.project(project_id)
        with self.db() as db: rows=db.execute('SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT 250',(project_id,after)).fetchall()
        return [{**dict(r),'data':json.loads(r['data'])} for r in rows]

    def claim(self):
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT id,request,attempt FROM projects WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:return None
            request=json.loads(row['request'])
            if request.get('interaction_mode')=='agent':
                from .conversation import claim_messages
                claim_messages(db,row['id'],row['attempt']+1,request)
            db.execute("UPDATE projects SET status='running',attempt=attempt+1,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",(now(),now(),row['id']))
        return self.project(row['id'])

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE workflow_nodes SET state='reconciling',blocker='worker interrupted; reconcile before resume',updated_at=? WHERE state='running'",(now(),))
            ids=[r['id'] for r in db.execute("SELECT id FROM projects WHERE status IN ('running','cancelling','rendering')")]
            db.execute("UPDATE projects SET status='interrupted',error='실행 서버가 재시작됐습니다. 원격 작업을 확인한 뒤 이어서 진행하세요.' WHERE status IN ('running','cancelling','rendering')")
            for pid in ids:db.execute("UPDATE chat_messages SET state='pending' WHERE project_id=? AND state='processing'",(pid,))
            archives=[r['id'] for r in db.execute("SELECT id FROM projects WHERE status='archiving'")]
            db.execute("UPDATE projects SET status='archive_failed',error='보관 중 서버가 재시작됐습니다. 보관을 다시 시도하면 저장된 체크포인트부터 확인합니다.' WHERE status='archiving'")
        for p in ids:self.event(p,'warning','중단된 실행을 보존했습니다. 자동으로 생성 요청을 반복하지 않습니다.')
        for p in archives:self.event(p,'warning','완성 영상을 보존했습니다. Google 보관을 다시 시도할 수 있습니다.')

    def connections(self):
        with self.db() as db: rows=db.execute('SELECT * FROM connections ORDER BY name').fetchall()
        return [{k:v for k,v in dict(r).items() if k!='secret'} | {'config':json.loads(r['config'])} for r in rows]

    def connection(self, connection_id, secret=False):
        with self.db() as db:r=db.execute('SELECT * FROM connections WHERE id=?',(connection_id,)).fetchone()
        if not r:raise Problem('연결을 찾을 수 없습니다.',404)
        obj=dict(r);obj['config']=json.loads(obj['config'])
        token=obj.pop('secret')
        if secret:obj['secret']=self.vault.decrypt(token).decode() if token else ''
        return obj

    def save_connection(self, data):
        cid=uid('conn')
        config=data.get('config',{})
        token=data.get('api_key','')
        with self.db() as db:
            db.execute('INSERT INTO connections VALUES(?,?,?,?,?,?,?,?,?)',
              (cid,data['name'],data['provider'],data['transport'],data['role'],json.dumps(config),self.vault.encrypt(token.encode()) if token else None,'saved',None))
        return self.connection(cid)

    def set_connection_status(self,cid,status):
        with self.db() as db:db.execute('UPDATE connections SET status=?,checked_at=? WHERE id=?',(status,now(),cid))

    def input(self,input_id):
        with self.db() as db:r=db.execute('SELECT * FROM inputs WHERE id=?',(input_id,)).fetchone()
        if not r:raise Problem('첨부 자료를 찾을 수 없습니다.',404)
        return dict(r)

    def project_root(self,project_id):
        self.project(project_id)
        with self.db() as db:row=db.execute('SELECT workspace FROM project_locations WHERE project_id=?',(project_id,)).fetchone()
        if row:
            from .workspaces import workspace_directory,checked_directory
            folder=workspace_directory(row['workspace'],self.root)
            target=checked_directory(str(folder/'pons-projects'/project_id))
            if not target.is_dir():raise Problem('root_missing: 작업 폴더가 이동되거나 삭제됐습니다. 기존 경로를 복원하세요.',409)
            return target
        return self.root/'projects'/project_id

def confined(root, relative, must_exist=True):
    root=Path(root).resolve();p=(root/relative).resolve()
    if not p.is_relative_to(root) or p==root:raise Problem('허용되지 않은 파일 경로입니다.',403)
    if must_exist and not p.is_file():raise Problem('파일을 찾을 수 없습니다.',404)
    return p

def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with open(temp,'w') as f:
        json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
