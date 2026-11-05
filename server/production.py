"""Validate a derived video handoff against the single canonical project.json."""
import hashlib
import json
import math
from pathlib import Path
from PIL import Image
from .store import Problem, confined

HANDOFF='06_GENERATION_SPECS/production_handoff.json'
CAMERA_FIELDS=('shot_size','angle','composition','movement','motivation','rhythm','continuity')


def required_text(value):
    return isinstance(value,str) and bool(value.strip()) and value.strip().lower() not in ('tbd','same','same as above','동일','위와 동일','미정')


def _inspect(root, required=False):
    root=Path(root)
    if not (root/HANDOFF).is_file():
        return {'available':False,'valid':False,'scenes':[],'shots':[],'errors':['씬별 스토리보드와 프레임 프롬프트 인계 파일이 필요합니다.'] if required else []}
    errors=[]
    try:
        path=confined(root,HANDOFF);d=json.loads(path.read_text());canonical=json.loads(confined(root,'project.json').read_text())
        if d.get('schema_version')!=1:errors.append('인계 스키마 버전은 1이어야 합니다.')
        scene_ids={s['id'] for s in canonical.get('scenes',[])}
        shots={s['id']:s for s in canonical.get('shots',[])}
        source_hash=hashlib.sha256(confined(root,'project.json').read_bytes()).hexdigest()
        if not shots:errors.append('정본에 컷이 없습니다.')
        if d.get('project_id')!=canonical.get('project_id'):errors.append('프로젝트 ID가 다릅니다.')
        # Bind to the source shot content, not the whole ledger which changes during QA.
        shot_hash=hashlib.sha256(json.dumps(canonical.get('shots',[]),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if d.get('source_shots_sha256')!=shot_hash:errors.append('정본 컷 버전이 바뀌었습니다. 연출·프레임 인계를 갱신하세요.')
        ds=d.get('scenes',[]);hs=d.get('shots',[])
        if not isinstance(ds,list) or not isinstance(hs,list):raise ValueError('scenes/shots must be arrays')
        if len({s.get('scene_id') for s in ds})!=len(ds) or {s.get('scene_id') for s in ds}!=scene_ids:errors.append('모든 씬을 중복 없이 인계해야 합니다.')
        if len({s.get('shot_id') for s in hs})!=len(hs) or {s.get('shot_id') for s in hs}!=set(shots):errors.append('모든 컷의 첫·마지막 프레임 프롬프트가 필요합니다.')
        coverage=set()
        for scene in ds:
            sid=scene.get('scene_id');sheets=scene.get('storyboard_sheets',[])
            if not isinstance(sheets,list) or not sheets:errors.append(f'{sid}: 실제 스토리보드 시트가 필요합니다.');continue
            for sheet in sheets:
                rel=sheet.get('path','')
                if any(part.startswith('.') for part in Path(rel).parts):raise ValueError('private sheet path')
                file=confined(root,rel)
                with Image.open(file) as img:img.verify()
                if sheet.get('sha256')!=hashlib.sha256(file.read_bytes()).hexdigest():errors.append(f'{sid}: 보드 시트 해시가 다릅니다.')
                ids=sheet.get('shot_ids',[])
                if not isinstance(ids,list) or not ids:errors.append(f'{sid}: 시트의 컷 연결이 없습니다.');continue
                for shot_id in ids:
                    if shot_id not in shots or shots[shot_id].get('scene_id')!=sid:errors.append(f'{sid}: 다른 씬의 컷 {shot_id}')
                    coverage.add(shot_id)
        if coverage!=set(shots):errors.append('보드 시트가 전체 컷을 포함하지 않습니다.')
        for shot in hs:
            sid=shot.get('shot_id');source=shots.get(sid,{})
            if shot.get('scene_id')!=source.get('scene_id'):errors.append(f'{sid}: 씬 연결 불일치')
            for key in ('first_frame_prompt','last_frame_prompt','motion_prompt','intent'):
                if not required_text(shot.get(key)):errors.append(f'{sid}: {key} 누락')
            camera=shot.get('camera',{})
            if not isinstance(camera,dict):camera={}
            for key in CAMERA_FIELDS:
                if not required_text(camera.get(key)):errors.append(f'{sid}: camera.{key} 누락')
            if shot.get('control_level')!='SOFT_PROMPT':errors.append(f'{sid}: 공급자 검증 전 연출 지시는 SOFT_PROMPT여야 합니다.')
            if shot.get('implementation_status') not in ('planned','animatic_static'):errors.append(f'{sid}: 계획과 실제 구현 상태를 구분하세요.')
            duration=shot.get('duration_s')
            expected=source.get('end_s',0)-source.get('start_s',0)
            if not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0 or abs(duration-expected)>.05:errors.append(f'{sid}: 컷 길이 불일치')
            phases=shot.get('timing',[])
            cursor=0.
            if not isinstance(phases,list) or not phases:errors.append(f'{sid}: 시작·진행·정착 타이밍 누락');continue
            for phase in phases:
                start=phase.get('start_s');end=phase.get('end_s')
                if not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or not math.isfinite(start) or not math.isfinite(end) or abs(start-cursor)>.001 or end<=start:
                    errors.append(f'{sid}: 연출 구간은 shot-local 초 기준으로 연속되어야 합니다.');break
                if not required_text(phase.get('action')):errors.append(f'{sid}: 시간 구간 행동 누락')
                cursor=end
            if isinstance(duration,(int,float)) and abs(cursor-duration)>.05:errors.append(f'{sid}: 연출 타이밍이 컷 길이와 다릅니다.')
        if not errors:
            for scene in ds:
                ordered=sorted((h for h in hs if h['scene_id']==scene['scene_id']),key=lambda h:shots[h['shot_id']]['start_s'])
                if ordered:
                    scene.update({'first_shot_id':ordered[0]['shot_id'],'last_shot_id':ordered[-1]['shot_id'],
                                  'first_frame_prompt':ordered[0]['first_frame_prompt'],'last_frame_prompt':ordered[-1]['last_frame_prompt'],
                                  'endpoint_source':'derived from canonical first/last shots; no separate image generation'})
        return {**d,'available':True,'valid':not errors,'errors':errors,'source_project_sha256':source_hash}
    except (Problem,ValueError,TypeError,KeyError,AttributeError,OSError,Image.UnidentifiedImageError) as e:
        return {'available':True,'valid':False,'scenes':[],'shots':[],'errors':['인계 파일을 확인하세요: '+str(e)[:200]]}


def inspect(root, required=False):
    from .storyboard import enrich
    return enrich(root,_inspect(root,required))


def validate(root):
    result=inspect(root,required=True)
    if not result['valid']:raise Problem('스토리보드·프레임 연출 인계를 보완하세요. '+'; '.join(result['errors'][:8]))
    return result
