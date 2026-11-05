"""Bounded video jobs through the installed CLI; operator approvals live outside agent workspace."""
import hashlib
import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path
from .store import Problem,atomic_json,confined,now
from . import media

_LOCK=threading.RLock()
MEDIA={'image_references','video_references','start_image','end_image','audio_references'}

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def area(store,pid):
    p=store.root/'video'/pid;p.mkdir(parents=True,exist_ok=True,mode=0o700);return p

def selected(store,request):
    if request.get('motion_video_authorized') is not True or request.get('delivery_kind','animatic')!='video_edit':raise Problem('이미지 애니매틱은 영상 생성 도구나 영상 크레딧을 사용하지 않습니다.',403)
    cid=request.get('connections',{}).get('video')
    if not cid:raise Problem('영상 도구를 선택하세요. 제작 설정에서 설치된 Higgsfield CLI를 연결할 수 있습니다.',409)
    c=store.connection(cid)
    if c['role']!='video' or c['transport']!='cli' or c['config'].get('runtime')!='higgsfield':raise Problem('현재 영상 실행 게이트는 설치된 Higgsfield CLI 영상 연결을 지원합니다.',409)
    return c

def cli(connection,args,timeout=90):
    command=connection['config']['command']
    try:r=subprocess.run([*command,*args,'--json'],capture_output=True,text=True,timeout=timeout)
    except subprocess.TimeoutExpired:raise Problem('영상 도구 응답 대기 시간이 초과됐습니다. 생성 제출은 상태를 확인하기 전 반복하지 마세요.',504)
    except OSError:raise Problem('설치된 영상 CLI를 실행할 수 없습니다.',502)
    if r.returncode:raise Problem('영상 도구 응답: '+r.stderr.strip()[:1000],502)
    try:return json.loads(r.stdout)
    except ValueError:raise Problem('영상 CLI의 구조화된 응답을 확인할 수 없습니다.',502)

def price(response):
    value=response.get('cost',response) if isinstance(response,dict) else {}
    for key in ('credits_exact','credits','cost'):
        n=value.get(key) if isinstance(value,dict) else None
        if type(n) in (int,float) and math.isfinite(n) and n>=0:return float(n)
    raise Problem('공급자가 확정 가능한 크레딧 견적을 반환하지 않았습니다.',502)

def quote_path(store,pid,qid=None):
    base=area(store,pid)
    if qid is None:
        index=base/'index.json'
        if not index.exists():return None
        qid=json.loads(index.read_text())['quote_id']
    if not isinstance(qid,str) or not re.fullmatch(r'vq_[a-f0-9]{32}',qid):raise Problem('영상 견적 ID를 확인하세요.')
    return base/(qid+'.json')

def cancel_pending(store,pid,reason):
    with _LOCK:
        for file in area(store,pid).glob('vq_*.json'):
            quote=json.loads(file.read_text())
            if not quote.get('approved_at') and all(x['state']=='quoted' for x in quote['items']):
                quote.update(cancelled_at=now(),cancellation_reason=reason);atomic_json(file,quote)
        index=area(store,pid)/'index.json'
        if index.exists():
            latest=json.loads(index.read_text())['quote_id'];quote=json.loads((area(store,pid)/(latest+'.json')).read_text())
            if quote.get('cancelled_at'):index.unlink()


def read_quote(store,pid,qid=None):
    p=quote_path(store,pid,qid)
    if not p or not p.exists():return None
    return json.loads(p.read_text())

def public_quote(q):
    if not q:return None
    return {k:q[k] for k in ('quote_id','created_at','expires_at','total_credits','approved_at','retry_budget') if k in q}|{'items':[{'index':i,'label':x['label'],'model':x['model'],'resolution':x['params'].get('resolution'),'source_duration_s':x.get('source_duration_s'),'image_count':len(x['params'].get('image_references',[])),'credits':x['credits'],'state':x.get('state','quoted')} for i,x in enumerate(q['items'])]}

def unchanged(root,quote):
    for path,sha in quote['input_hashes'].items():
        if digest(confined(root,path))!=sha:raise Problem('견적 이후 입력 파일이 바뀌었습니다. 새 견적을 확인해주세요.',409)

def arguments(root,item,schema,hashes=None):
    allowed={p['name']:p for p in schema.get('params',[])}
    params=item.get('params',{})
    if not isinstance(params,dict) or not set(params)<=set(allowed):raise Problem('현재 모델이 지원하는 매개변수만 사용할 수 있습니다.')
    result=[]
    for name,value in params.items():
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',name):raise Problem('영상 매개변수를 확인하세요.')
        if 'enum' in allowed[name] and value not in allowed[name]['enum']:raise Problem('모델이 지원하는 설정값을 선택하세요.')
        if name in MEDIA:
            values=value if isinstance(value,list) else [value]
            if not 1<=len(values)<=10:raise Problem('영상 참조 자료 개수를 확인하세요.')
            for rel in values:
                if not isinstance(rel,str) or any(x.startswith('.') for x in Path(rel).parts):raise Problem('프로젝트 안의 참조 파일을 지정하세요.')
                file=confined(root,rel)
                if not file.is_file():raise Problem('영상 참조 파일을 찾을 수 없습니다.')
                if hashes is not None:hashes[rel]=digest(file)
                result.append('--'+name.replace('_','-')+'='+str(file))
        else:
            if not isinstance(value,(str,int,float,bool)) or isinstance(value,float) and not math.isfinite(value):raise Problem('영상 매개변수 형식이 올바르지 않습니다.')
            if isinstance(value,str) and len(value)>20000:raise Problem('영상 프롬프트가 너무 깁니다.')
            result.append('--'+name.replace('_','-')+'='+(str(value).lower() if isinstance(value,bool) else str(value)))
    for name,spec in allowed.items():
        if spec.get('required') and name not in params:raise Problem('필수 영상 매개변수가 없습니다: '+name)
    if item['model'].startswith('hf_mult_replace_object') or item['model'].startswith('hf_mult_motion_control'):
        videos=params.get('video_references',[]);images=params.get('image_references',[])
        if not isinstance(videos,list) or len(videos)!=1 or not isinstance(images,list) or not images:raise Problem('인물 교체에는 원본 영상 1개와 인물 이미지가 필요합니다.')
        info=media.probe(confined(root,videos[0]));duration=float(info['format']['duration'])
        if not 4<=duration<=30:raise Problem('이 영상 모델의 입력은 4~30초입니다. 원본을 적절한 구간으로 나눠주세요.')
    return result

def make_quote(store,job,plan,redact=lambda s:s):
    root=store.project_root(job['id']);connection=selected(store,job['request'])
    if not isinstance(plan,list) or not 1<=len(plan)<=30:raise Problem('견적에는 최대 30개의 구체적인 영상 작업을 넣으세요.')
    hashes={};items=[]
    scope=[m.get('id') for m in job['request'].get('agent_current_messages',[])]
    existing=read_quote(store,job['id'])
    if existing and (existing.get('scope')==scope or existing.get('approval_message_id') in scope) and existing.get('connection_id')==connection['id'] and len(existing['items'])==len(plan) and all(a.get('model')==b.get('model') and a.get('params')==b.get('params') and str(a.get('label') or f'구간 {i+1}')[:180]==b.get('label') for i,(a,b) in enumerate(zip(plan,existing['items']))) and (existing.get('approved_at') or existing['expires_at']>time.time()):
        unchanged(root,existing);return public_quote(existing)
    for index,item in enumerate(plan):
        store.event(job['id'],'tool',f'영상 구간 {index+1}/{len(plan)}의 실제 견적을 확인하고 있습니다. 생성은 제출하지 않습니다.')
        if not isinstance(item,dict) or not re.fullmatch(r'[a-zA-Z0-9_]{1,100}',item.get('model','')):raise Problem('실제 영상 모델 ID를 지정하세요.')
        schema=cli(connection,['model','get',item['model']])
        if schema.get('type')!='video':raise Problem('영상 모델만 이 실행 경로에서 사용할 수 있습니다.')
        args=arguments(root,item,schema,hashes)
        cost=price(cli(connection,['generate','cost',item['model'],*args]))
        videos=item['params'].get('video_references',[])
        duration=float(media.probe(confined(root,videos[0]))['format']['duration']) if videos else None
        items.append({'model':item['model'],'params':item['params'],'label':str(item.get('label') or f'구간 {index+1}')[:180],'credits':cost,'source_duration_s':duration,'state':'quoted'})
    payload={'connection_id':connection['id'],'scope':scope,'items':[{k:v for k,v in x.items() if k!='state'} for x in items],'input_hashes':hashes,'retry_budget':0}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    qid='vq_'+fingerprint[:32]
    previous=read_quote(store,job['id'],qid)
    if previous and (previous.get('approved_at') or previous['expires_at']>time.time()):return public_quote(previous)
    q={'quote_id':qid,'created_at':now(),'expires_at':time.time()+1800,'total_credits':sum(x['credits'] for x in items),'approved_at':None,**payload,'items':items}
    atomic_json(quote_path(store,job['id'],qid),q);atomic_json(area(store,job['id'])/'index.json',{'quote_id':qid})
    store.event(job['id'],'state',f'영상 {len(items)}개 구간의 견적을 준비했습니다. 총 {q["total_credits"]:g} 크레딧이며 실행 승인을 기다립니다.',{'video_quote_id':qid})
    return public_quote(q)

def approve(store,pid,qid):
    if not isinstance(qid,str) or not re.fullmatch(r'vq_[a-f0-9]{32}',qid):raise Problem('표시된 영상 견적 ID가 필요합니다.')
    with _LOCK:
        job=store.project(pid);q=read_quote(store,pid,qid)
        if not q:raise Problem('실제 영상 견적이 없습니다.',404)
        if q.get('cancelled_at'):raise Problem('취소된 동영상 견적입니다. 이미지 애니매틱에는 적용하지 않습니다.',409)
        if q.get('approved_at'):return {'quote_id':qid,'approved':True,'already_approved':True}
        if job['status'] in ('running','queued','cancelling','archiving'):raise Problem('현재 작업이 끝난 뒤 견적을 승인하세요.',409)
        if q['expires_at']<time.time():raise Problem('견적이 만료됐습니다. 새 견적을 요청하세요.',409)
        if selected(store,job['request'])['id']!=q['connection_id']:raise Problem('영상 연결이 바뀌었습니다. 새 견적을 요청하세요.',409)
        unchanged(store.project_root(pid),q)
        q['approved_at']=now();atomic_json(quote_path(store,pid,qid),q)
        from .conversation import post_message
        try:
            receipt=post_message(store,pid,{'message':f'표시된 영상 견적 {qid}의 {len(q["items"])}개 작업, 합계 {q["total_credits"]:g} 크레딧, 재시도 0회를 승인합니다. 같은 요청의 인물 교체 재구성을 이어서 진행해주세요.','client_message_id':'approve-'+qid,'execution_authorized':True})
            q['approval_message_id']=receipt['id'];atomic_json(quote_path(store,pid,qid),q)
        except Exception:q['approved_at']=None;atomic_json(quote_path(store,pid,qid),q);raise
        store.event(pid,'state','사용자가 표시된 영상 견적과 작업 범위의 실행을 승인했습니다.',{'video_quote_id':qid})
        return {'quote_id':qid,'approved':True}

def action(store,job,data,redact=lambda s:s):
    connection=selected(store,job['request']);root=store.project_root(job['id']);op=data.get('operation')
    if op=='models':
        rows=cli(connection,['model','list'])
        return [r for r in rows if r.get('type')=='video']
    if op=='inspect':
        model=data.get('model','')
        if not re.fullmatch(r'[a-zA-Z0-9_]{1,100}',model):raise Problem('영상 모델 ID를 확인하세요.')
        return cli(connection,['model','get',model])
    with _LOCK:
        if op=='quote':return make_quote(store,job,data.get('items'),redact)
        q=read_quote(store,job['id'],data.get('quote_id'))
        if not q:raise Problem('검증한 영상 견적이 없습니다.',404)
        if op=='status':
            index=data.get('index')
            if index is None:return public_quote(q)
            if type(index) is not int or not 0<=index<len(q['items']):raise Problem('영상 작업 번호를 확인하세요.')
            return q['items'][index]
        if op!='submit':raise Problem('영상 작업 종류를 확인하세요.')
        if not q.get('approved_at'):raise Problem('영상 견적 카드에서 사용자가 실행을 승인해야 합니다.',403)
        if q.get('approval_message_id') not in [m.get('id') for m in job['request'].get('agent_current_messages',[])]:raise Problem('현재 요청에 연결된 견적 승인이 아닙니다. 변경된 요청은 새 견적을 확인해주세요.',409)
        if q['expires_at']<time.time() and all(x['state']=='quoted' for x in q['items']):raise Problem('첫 실행 전에 견적이 만료됐습니다. 새 견적이 필요합니다.',409)
        if q['connection_id']!=connection['id']:raise Problem('승인한 영상 도구와 현재 연결이 다릅니다.',409)
        unchanged(root,q);index=data.get('index')
        if type(index) is not int or not 0<=index<len(q['items']):raise Problem('영상 작업 번호를 확인하세요.')
        item=q['items'][index]
        if item['state']!='quoted':return item  # Never repeat an uncertain/failed/completed paid submission.
        schema=cli(connection,['model','get',item['model']]);args=arguments(root,item,schema)
        current=price(cli(connection,['generate','cost',item['model'],*args]))
        if current>item['credits']+1e-8:raise Problem('공급자 견적이 승인 금액보다 올랐습니다. 새 견적 승인이 필요합니다.',409)
        if store.project(job['id'])['status']!='running':raise Problem('실행이 중지됐습니다. 영상 생성은 제출하지 않았습니다.',409)
        item.update(state='submission_unknown',submitted_at=now());atomic_json(quote_path(store,job['id'],q['quote_id']),q)
        store.event(job['id'],'tool',f'승인된 영상 구간 {index+1}/{len(q["items"])}을 제출합니다.',{'video_quote_id':q['quote_id']})
        try:
            response=cli(connection,['generate','create',item['model'],*args,'--wait','--wait-timeout','45m','--wait-interval','10s'],timeout=2750)
            item['provider_result']=response;item['state']='response_received'
        except Problem as e:
            item['error']=redact(e.message);atomic_json(quote_path(store,job['id'],q['quote_id']),q)
            raise Problem('영상 제출 결과를 확인해야 합니다. 자동 재제출하지 않습니다. '+item['error'],502)
        atomic_json(quote_path(store,job['id'],q['quote_id']),q);return item


def completed_job_ids(response):
    found=set()
    def visit(value):
        if isinstance(value,dict):
            identity=value.get('id') or value.get('job_id')
            if identity and value.get('status') in ('completed','succeeded','success') and any(value.get(k) for k in ('result_url','results','result','output','url')):found.add(str(identity))
            for item in value.values():visit(item)
        elif isinstance(value,list):
            for item in value:visit(item)
    visit(response);return found


def validate_delivery(store,root,request):
    """Video edits preserve source audio/caption intent; they need no invented narration."""
    if request.get('production_contract_version',1)>=2:
        from .production import validate
        validate(root)
    result=json.loads(confined(root,'10_DELIVERY/render_result.json').read_text())
    if result.get('kind')!='video_edit':raise Problem('영상 편집 납품 종류가 일치하지 않습니다.')
    video=confined(root,result.get('video',''));sha=digest(video)
    if sha!=result.get('sha256'):raise Problem('최종 영상의 해시가 일치하지 않습니다.')
    info=media.probe(video)
    if not any(s['codec_type']=='video' for s in info['streams']):raise Problem('실제 영상 스트림이 필요합니다.')
    actual=float(info['format']['duration']);target=request['target_duration_s']
    if not math.isfinite(actual) or abs(actual-target)>max(2,target*.03):raise Problem('최종 영상 길이가 목표 범위를 벗어났습니다.')
    qa=json.loads(confined(root,'09_QA/qa.json').read_text())
    if qa.get('video_sha256')!=sha:raise Problem('현재 최종 영상에 대한 검수 근거가 필요합니다.')
    checks={x.get('category'):x for x in qa.get('checks',[])}
    for category in ('identity','visual','continuity','playback','timing'):
        x=checks.get(category,{})
        if x.get('status')!='pass' or not x.get('evidence'):raise Problem('필수 영상 검수 근거가 없습니다: '+category)
    for category in ('audio','captions'):
        x=checks.get(category,{})
        if x.get('status') not in ('pass','not_applicable') or not x.get('evidence'):raise Problem('오디오·자막의 실제 상태와 근거를 기록하세요.')
        if category=='audio' and any(s['codec_type']=='audio' for s in info['streams']) and x.get('status')!='pass':raise Problem('음성·음악이 있는 영상의 오디오 검수가 필요합니다.')
    q=read_quote(store,Path(root).name,result.get('video_quote_id'))
    if not q or not q.get('approved_at') or any(x['state']!='response_received' for x in q['items']):raise Problem('승인된 영상 작업의 실제 결과를 확인해야 합니다.')
    unchanged(root,q)
    joins=result.get('generation_results',[])
    if len(joins)!=len(q['items']):raise Problem('각 생성 구간의 실제 공급자 결과가 필요합니다.')
    for index,entry in enumerate(joins):
        if entry.get('index')!=index or not entry.get('provider_job_id'):raise Problem('공급자 작업 ID와 구간 연결이 필요합니다.')
        file=confined(root,entry.get('file',''))
        if digest(file)!=entry.get('sha256'):raise Problem('생성 구간 파일 해시를 확인하세요.')
        observed=media.probe(file)
        if not any(s['codec_type']=='video' for s in observed['streams']):raise Problem('생성 구간에 실제 영상이 필요합니다.')
        if entry['provider_job_id'] not in completed_job_ids(q['items'][index].get('provider_result',{})):raise Problem('공급자의 실제 완료 응답과 구간의 작업 ID가 다릅니다.')
    canonical=json.loads(confined(root,'project.json').read_text())
    asset=next((a for a in canonical.get('asset_registry',[]) if a.get('id')==canonical.get('delivery_spec',{}).get('final_asset_id')),None)
    if not asset or asset.get('sha256')!=sha or confined(root,asset.get('path',''))!=video:raise Problem('정본의 최종 영상 연결을 확인하세요.')
    if not list((Path(root)/'.history/events').glob('*.md')):raise Problem('실제 제작 이력이 필요합니다.')
    decoded=subprocess.run(['ffmpeg','-v','error','-i',str(video),'-f','null','-'],capture_output=True,timeout=180)
    if decoded.returncode:raise Problem('최종 영상 전체 디코딩에 실패했습니다.')
    from .storyboard import write_export
    document=write_export(root) if request.get('production_contract_version',1)>=2 else None
    return {**result,**({'storyboard_design':document} if document else {}),'duration_s':actual,'qa':qa,'local_only':not request.get('drive'),'technical_checks':'pass'}
