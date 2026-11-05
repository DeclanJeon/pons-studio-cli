"""Persistent project chat. User text is CLI stdin, never a shell command."""
import hashlib
import json
from .store import Problem, now, uid
from .system_integration import DEFAULT_IMAGE_EXECUTOR


def new_request(store, data):
    message=data.get('message','')
    if not isinstance(message,str) or not message.strip() or len(message)>10000:raise Problem('메시지는 1~10,000자로 입력하세요.')
    options=data.get('options',{})
    if not isinstance(options,dict):raise Problem('제작 설정을 확인하세요.')
    speech=next((c['id'] for c in store.connections() if c['role']=='speech' and c['provider']=='typecast' and c['status'] in ('ready','verified','authenticated')),None)
    if not speech:speech=next((c['id'] for c in store.connections() if c['role']=='speech' and c['status'] in ('ready','verified','authenticated')),None)
    overrides=options.get('connections',{})
    if not isinstance(overrides,dict) or not set(overrides)<= {'speech','image','research','video'}:raise Problem('제작 도구 선택을 확인하세요.')
    motion=options.get('motion_video_authorized') is True
    selected={'speech':speech,'image':DEFAULT_IMAGE_EXECUTOR,**overrides}
    if not motion:selected.pop('video',None)
    if motion and not selected.get('video'):raise Problem('생성형 동영상을 명시적으로 선택한 경우 영상 도구도 선택하세요.')
    return {'user_prompt':message,'title':message.strip().splitlines()[0][:60],
            'interaction_mode':'agent','production_contract_version':2,'delivery_kind':'video_edit' if motion else 'animatic','motion_video_authorized':motion,
            'target_duration_s':options.get('target_duration_s',60),'aspect_ratio':options.get('aspect_ratio','9:16'),
            'language':options.get('language','ko'),'visual_style':options.get('visual_style','자동 설계'),
            **({'workspace':data['workspace']} if 'workspace' in data else {}),
            **({'workflow_enabled':True,'recipe':'generated_motion' if motion else 'narrated_stills'} if data.get('workflow_enabled') is True else {}),
            'inputs':data.get('inputs',[]),'reference_urls':data.get('reference_urls',[]),'connections':selected,
            'voice_id':options.get('voice_id'),'creative_delegation':['plot','characters','voices','visual_style','cinematography'],
            'execution_authorized':data.get('execution_authorized') is True,'drive':options.get('drive',False) is True,
            'limits':{'wall_time_s':3600,'max_tts_characters':10000,'max_tts_calls':30}}


def post_message(store, pid, data, defer_active=False):
    message=data.get('message','');client_id=data.get('client_message_id','')
    if not isinstance(message,str) or not message.strip() or len(message)>10000:raise Problem('메시지는 1~10,000자로 입력하세요.')
    if not isinstance(client_id,str) or not 1<=len(client_id)<=200:raise Problem('메시지 식별자가 필요합니다.')
    inputs=data.get('inputs',[]);urls=data.get('reference_urls',[])
    if not isinstance(inputs,list) or not isinstance(urls,list):raise Problem('첨부 자료를 확인하세요.')
    meta={'inputs':inputs,'reference_urls':urls,'execution_authorized':data.get('execution_authorized') is True}
    fingerprint=hashlib.sha256(json.dumps([message,meta],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with store.lock,store.db() as db:
        if store.closing:raise Problem('제작실이 종료 중입니다. 다시 실행한 뒤 요청하세요.',409)
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM chat_messages WHERE project_id=? AND client_id=?',(pid,client_id)).fetchone()
        if row:
            if row['fingerprint']!=fingerprint:raise Problem('같은 메시지 식별자에 다른 내용이 전달됐습니다.',409)
            return {'id':row['id'],'state':row['state']}
        project=db.execute('SELECT status,request FROM projects WHERE id=?',(pid,)).fetchone()
        if not project:raise Problem('대화를 찾을 수 없습니다.',404)
        req=json.loads(project['request'])
        if req.get('interaction_mode')!='agent':raise Problem('이 제작은 에이전트 대화가 아닙니다.',409)
        if db.execute("SELECT COUNT(*) FROM chat_messages WHERE project_id=? AND state='pending'",(pid,)).fetchone()[0]>=10:raise Problem('대기 메시지가 많습니다. 현재 작업이 끝난 뒤 보내주세요.',409)
        pending=db.execute("SELECT meta FROM chat_messages WHERE project_id=? AND state IN ('pending','processing')",(pid,)).fetchall()
        all_inputs=[*req.get('inputs',[]),*inputs];all_urls=[*req.get('reference_urls',[]),*urls]
        for item in pending:
            existing=json.loads(item['meta']);all_inputs.extend(existing.get('inputs',[]));all_urls.extend(existing.get('reference_urls',[]))
        all_inputs=list(dict.fromkeys(all_inputs));all_urls=list(dict.fromkeys(all_urls))
        if len(all_inputs)>10 or len(all_urls)>10:raise Problem('대화의 첨부 파일과 참고 URL은 각각 최대 10개입니다.')
        if sum(store.input(i)['size'] for i in all_inputs)>300*1024*1024:raise Problem('대화 첨부 자료 합계는 300 MB 이하여야 합니다.')
        mid=uid('msg')
        meta['wake_after_failure']=project['status'] in ('failed','interrupted','cancelled')
        db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?)',(mid,pid,client_id,message,json.dumps(meta,ensure_ascii=False),fingerprint,now(),'pending',None))
        if not defer_active and project['status'] not in ('running','queued','cancelling','archiving'):
            db.execute("UPDATE projects SET status='queued',error=NULL,started_at=NULL,agent_outcome=NULL,repair_count=0,updated_at=? WHERE id=?",(now(),pid))
    store.event(pid,'chat','새 메시지를 접수했습니다. 현재 작업이 있으면 완료 후 이어서 전달합니다.',{'message_id':mid})
    return {'id':mid,'state':'pending'}


def claim_messages(db, pid, attempt, request):
    rows=db.execute("SELECT * FROM chat_messages WHERE project_id=? AND state='processing' ORDER BY at,id",(pid,)).fetchall()
    if not rows:
        candidates=db.execute("SELECT * FROM chat_messages WHERE project_id=? AND state='pending' ORDER BY at,id",(pid,)).fetchall()
        rows=sorted(candidates,key=lambda r:(not json.loads(r['meta']).get('wake_after_failure',False),r['at'],r['id']))[:1]
    if not rows:return request
    active=[];inputs=list(request.get('inputs',[]));urls=list(request.get('reference_urls',[]));grants=[]
    for row in rows:
        meta=json.loads(row['meta']);inputs.extend(meta.get('inputs',[]));urls.extend(meta.get('reference_urls',[]));grants.append(meta.get('execution_authorized') is True)
        active.append({'id':row['id'],'message':row['content'],'inputs':meta.get('inputs',[]),'reference_urls':meta.get('reference_urls',[]),'execution_authorized':meta.get('execution_authorized') is True})
        db.execute("UPDATE chat_messages SET state='processing',attempt=? WHERE id=?",(attempt,row['id']))
    request.update({'agent_current_messages':active,'inputs':list(dict.fromkeys(inputs)),'reference_urls':list(dict.fromkeys(urls)),'execution_authorized':all(grants)})
    db.execute('UPDATE projects SET request=?,agent_outcome=NULL WHERE id=?',(json.dumps(request,ensure_ascii=False),pid))
    return request


def finish_turn(store,pid):
    with store.lock,store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT status,request FROM projects WHERE id=?',(pid,)).fetchone()
        if not row or json.loads(row['request']).get('interaction_mode')!='agent':return
        status=row['status']
        if status=='queued':return  # bounded delivery repair keeps ownership of this batch
        success=status in ('awaiting_input','animatic_ready','completed','archive_failed')
        db.execute("UPDATE chat_messages SET state=? WHERE project_id=? AND state='processing'",('done' if success else 'failed',pid))
        pending=db.execute("SELECT meta FROM chat_messages WHERE project_id=? AND state='pending'",(pid,)).fetchall()
        explicit_retry=any(json.loads(r['meta']).get('wake_after_failure') for r in pending)
        if pending and (success or explicit_retry):
            db.execute("UPDATE projects SET status='queued',started_at=NULL,agent_outcome=NULL,error=NULL,repair_count=0,updated_at=? WHERE id=?",(now(),pid))


def messages(store,pid):
    store.project(pid)
    with store.db() as db:
        rows=db.execute('SELECT * FROM chat_messages WHERE project_id=? ORDER BY at,id',(pid,)).fetchall()
        events=db.execute("SELECT * FROM events WHERE project_id=? AND kind='agent' ORDER BY seq",(pid,)).fetchall()
    result=[]
    for row in rows:
        meta=json.loads(row['meta']);attached=[]
        for iid in meta.get('inputs',[]):
            inp=store.input(iid);attached.append({k:inp[k] for k in ('id','name','size','mime')})
        result.append({'id':row['id'],'role':'user','content':row['content'],'at':row['at'],'state':row['state'],'inputs':attached,'reference_urls':meta.get('reference_urls',[]),'client_message_id':row['client_id'],'source':meta.get('source','chat')})
    result.extend({'id':'event_'+str(e['seq']),'role':'assistant','content':e['message'],'at':e['at'],'state':'done','inputs':[],'reference_urls':[]} for e in events)
    return sorted(result,key=lambda x:(x['at'],x['id']))


def attach_existing(store,pid):
    with store.lock,store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
        if not row:raise Problem('제작을 찾을 수 없습니다.',404)
        if row['status'] in ('queued','running','cancelling','archiving'):raise Problem('현재 작업이 끝난 뒤 대화로 이어갈 수 있습니다.',409)
        request=json.loads(row['request'])
        if request.get('interaction_mode')=='agent':return store.project(pid)
        request.update({'interaction_mode':'agent','production_contract_version':2})
        db.execute('UPDATE projects SET request=?,updated_at=? WHERE id=?',(json.dumps(request,ensure_ascii=False),now(),pid))
        meta={'inputs':request.get('inputs',[]),'reference_urls':request.get('reference_urls',[]),'execution_authorized':request.get('execution_authorized') is True,'source':'existing_project_request'}
        db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?)',(uid('msg'),pid,'existing_project_request',request['user_prompt'],json.dumps(meta,ensure_ascii=False),hashlib.sha256(request['user_prompt'].encode()).hexdigest(),row['created_at'],'done',row['attempt']))
    store.event(pid,'state','기존 제작과 Codex 세션을 보존해 에이전트 대화로 연결했습니다. 새 실행은 메시지를 보낼 때 시작합니다.')
    return store.project(pid)
