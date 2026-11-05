"""Storyboard timeline/document projection. project.json stays the content authority."""
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from urllib.parse import quote
from .store import Problem,confined

EXPORT='06_GENERATION_SPECS/storyboard_design.md'

def timecode(seconds):
    milliseconds=round(float(seconds)*1000);whole,ms=divmod(milliseconds,1000);minutes,sec=divmod(whole,60);hours,minute=divmod(minutes,60)
    return f'{hours:02}:{minute:02}:{sec:02}.{ms:03}'

def timeline(canonical):
    if not isinstance(canonical,dict) or not isinstance(canonical.get('scenes',[]),list) or not isinstance(canonical.get('shots',[]),list):raise Problem('정본의 씬·컷 목록 형식을 확인하세요.',409)
    errors=[];rows=[];scene_map={s.get('id'):s for s in canonical.get('scenes',[]) if isinstance(s,dict)}
    fps=canonical.get('fps',24)
    if type(fps) not in (int,float) or not math.isfinite(fps) or not 1<=fps<=120:fps=24
    for s in canonical.get('shots',[]):
        if not isinstance(s,dict):errors.append('컷 형식을 확인하세요.');continue
        start,end=s.get('start_s'),s.get('end_s')
        if type(start) not in (int,float) or type(end) not in (int,float) or not math.isfinite(start) or not math.isfinite(end) or start<0 or end<=start:
            errors.append(str(s.get('id','컷'))+': 유효한 전체 영상 시작·종료 시각이 필요합니다.');continue
        if s.get('scene_id') not in scene_map:errors.append(str(s.get('id'))+': 정본 씬 연결을 확인하세요.')
        rows.append({'shot_id':s.get('id'),'scene_id':s.get('scene_id'),'start_s':start,'end_s':end,'duration_s':end-start,'start_tc':timecode(start),'end_tc':timecode(end),
                     'start_frame':round(start*fps),'end_frame':round(end*fps),'frame_basis':'derived from canonical seconds at project fps',
                     'intent':s.get('purpose') or s.get('intent') or '', 'transition':s.get('transition') or '전환 효과 미지정',
                     'source_in_s':s.get('source_in_s'),'source_out_s':s.get('source_out_s')})
    rows.sort(key=lambda s:(s['start_s'],str(s['shot_id'])))
    if len({r['shot_id'] for r in rows})!=len(rows) or any(not r['shot_id'] for r in rows):errors.append('컷 ID는 고유하고 비어 있지 않아야 합니다.')
    if rows and abs(rows[0]['start_s'])>.001:errors.append('전체 타임라인은 0초부터 시작해야 합니다.')
    for index,r in enumerate(rows):
        prior=rows[index-1] if index else None;following=rows[index+1] if index+1<len(rows) else None
        r.update(previous_shot_id=prior['shot_id'] if prior else None,next_shot_id=following['shot_id'] if following else None)
        if following and abs(r['end_s']-following['start_s'])>.001:errors.append(f'{r["shot_id"]} → {following["shot_id"]}: 컷 구간에 빈 구간 또는 겹침이 있습니다.')
    scenes=[]
    for sid,s in scene_map.items():
        cuts=[r for r in rows if r['scene_id']==sid]
        if not cuts:continue
        # A repeated scene may have several disjoint intervals; never hide intervening scenes.
        intervals=[]
        for cut in cuts:
            if intervals and abs(intervals[-1]['end_s']-cut['start_s'])<=.001:intervals[-1]['end_s']=cut['end_s'];intervals[-1]['shot_ids'].append(cut['shot_id'])
            else:intervals.append({'start_s':cut['start_s'],'end_s':cut['end_s'],'shot_ids':[cut['shot_id']]})
        scenes.append({'scene_id':sid,'title':s.get('title') or sid,'intent':s.get('purpose') or s.get('intent') or '', 'start_s':cuts[0]['start_s'],'end_s':cuts[-1]['end_s'],
                       'duration_s':sum(c['duration_s'] for c in cuts),'intervals':intervals,'shot_ids':[c['shot_id'] for c in cuts]})
    scenes.sort(key=lambda s:s['start_s'])
    return {'available':bool(rows),'valid':bool(rows) and not errors,'errors':errors,'fps':fps,'total_duration_s':rows[-1]['end_s'] if rows else 0,'time_basis':'global project seconds; half-open intervals','scenes':scenes,'shots':rows}

def load(root):
    path=confined(root,'project.json')
    try:canonical=json.loads(path.read_text())
    except (OSError,ValueError):raise Problem('정본 제작 설계가 아직 준비되지 않았습니다.',409)
    return canonical,timeline(canonical)

def enrich(root,result):
    try:
        canonical,t=load(root)
    except Problem:return {**result,'timeline':{'available':False,'valid':False,'shots':[],'scenes':[],'errors':[]}}
    result={**result,'timeline':t,'markdown_available':t['available'],'markdown_path':EXPORT}
    if not t['valid']:
        result.update(valid=False,errors=[*result.get('errors',[]),*(t['errors'] or ['정본 컷 타임라인이 필요합니다.'])])
        return result
    timings={s['shot_id']:s for s in t['shots']};scenes={s['scene_id']:s for s in t['scenes']}
    for shot in result.get('shots',[]):
        if shot.get('shot_id') in timings:shot.update({k:v for k,v in timings[shot['shot_id']].items() if k not in ('intent','transition')})
    for scene in result.get('scenes',[]):
        if scene.get('scene_id') in scenes:
            scene.update({k:v for k,v in scenes[scene['scene_id']].items() if k not in ('intent','title')})
        for sheet in scene.get('storyboard_sheets',[]):sheet['timeline']=[timings[s] for s in sheet.get('shot_ids',[]) if s in timings]
    return result

def cell(value):return str(value if value is not None else '').replace('|','\\|').replace('\r','').replace('\n','<br>')
def link(path):return '../'+quote(str(path),safe='/._-')

def _markdown(root):
    from .production import inspect
    canonical,t=load(root)
    if not t['available']:raise Problem('정본에 씬·컷 시간 정보가 아직 없습니다.',409)
    data=inspect(root);complete=data.get('valid') is True
    state='완성된 인계에서 내보낸 설계서' if complete else '미완성 설계 초안 · 이미지/시트/프레임 설계의 미작성 항목이 남아 있습니다'
    plan={s['shot_id']:s for s in data.get('shots',[]) if isinstance(s,dict)} if data.get('valid') else {}
    shots={s['id']:s for s in canonical.get('shots',[]) if isinstance(s,dict)}
    sheets={s['scene_id']:s.get('storyboard_sheets',[]) for s in data.get('scenes',[]) if isinstance(s,dict)} if complete else {}
    project_hash=hashlib.sha256(confined(root,'project.json').read_bytes()).hexdigest()
    lines=['# 스토리보드 설계서','',f'- 프로젝트: {cell(canonical.get("project_id",""))}',f'- 상태: {state}',f'- 기준: 전체 영상 00:00:00.000부터 · 시작 포함/끝 제외',f'- 총 길이: {t["total_duration_s"]:.3f}초 · {t["fps"]:g}fps',f'- 제작 방식: {"생성형 동영상" if canonical.get("motion_video_authorized") else "정지 이미지 애니매틱"}',
           '- 카메라·동작 프롬프트는 연출 계획입니다. 실제 구현 여부는 검수 기록으로 구분합니다.','', '## 요청','',str(canonical.get('user_prompt','')),'','## 씬 타임라인','', '| 씬 | 구간(전체 영상) | 실제 포함 길이 | 컷 | 의도 |','| --- | --- | ---: | --- | --- |']
    for scene in t['scenes']:
        ranges=' / '.join(timecode(i['start_s'])+'–'+timecode(i['end_s']) for i in scene['intervals'])
        lines.append(f'| {cell(scene["scene_id"])} | {ranges} | {scene["duration_s"]:.3f}초 | {cell(", ".join(scene["shot_ids"]))} | {cell(scene["intent"])} |')
    lines+=['','## 컷 타임라인','', '| 씬 / 컷 | 시작 | 종료 | 길이 | 다음 컷 / 연결 시점 | 컷 의도 |','| --- | --- | --- | ---: | --- | --- |']
    for row in t['shots']:
        nxt=(str(row['next_shot_id'])+' @ '+row['end_tc']) if row['next_shot_id'] else '영상 종료'
        lines.append(f'| {cell(row["scene_id"])} / {cell(row["shot_id"])} | {row["start_tc"]} | {row["end_tc"]} | {row["duration_s"]:.3f}초 | {cell(nxt)} | {cell(row["intent"])} |')
    reused=canonical.get('editorial_reuse',[])
    if isinstance(reused,list) and reused:
        lines+=['','## 기존 이미지 재사용 구간','', '| 컷 | 원본 완성 컷 | 편집 이유 | 결정 근거 |','| --- | --- | --- | --- |']
        for reuse in reused:
            if isinstance(reuse,dict):lines.append(f'| {cell(reuse.get("shot_id"))} | {cell(reuse.get("donor_shot_id"))} | {cell(reuse.get("reason"))} | {cell(reuse.get("authority"))} |')
        lines+=['','재사용은 이미 완성된 정지 이미지의 편집입니다. 거절된 이미지의 새 생성 성공을 의미하지 않습니다.']
    if t['errors']:lines+=['','### 타임라인 검사에서 보완할 항목','',*['- '+e for e in t['errors']]]
    for scene in t['scenes']:
        lines+=['',f'## {scene["scene_id"]} · {cell(scene["title"])}','',scene['intent'],'']
        boards=sheets.get(scene['scene_id'],[])
        if not boards:lines+=['보드 시트: 아직 완성·검증되지 않았습니다.','']
        for board in boards:
            lines+=[f'![{cell(scene["scene_id"])} 스토리보드]({link(board["path"])})','',f'[시트 파일]({link(board["path"])})','', '| 컷 | 전체 영상 구간 | 길이 |','| --- | --- | ---: |']
            for row in t['shots']:
                if row['shot_id'] in board['shot_ids']:lines.append(f'| {cell(row["shot_id"])} | {row["start_tc"]}–{row["end_tc"]} | {row["duration_s"]:.3f}초 |')
            lines.append('')
        for row in t['shots']:
            if row['scene_id']!=scene['scene_id']:continue
            detail=plan.get(row['shot_id'],{});source=shots[row['shot_id']];camera=detail.get('camera') or source.get('camera') or {}
            if not isinstance(camera,dict):camera={}
            lines+=[f'### {row["shot_id"]} · {row["start_tc"]}–{row["end_tc"]} ({row["duration_s"]:.3f}초)','',row['intent'] or '컷 의도: 미작성','',f'- 이전 컷: {row["previous_shot_id"] or "영상 시작"}',f'- 다음 컷: {row["next_shot_id"] or "영상 종료"}',f'- 전환: {cell(row["transition"])}']
            for key,label in [('shot_size','샷 크기'),('angle','앵글'),('composition','구도'),('movement','카메라 무빙'),('motivation','선택 이유'),('rhythm','리듬'),('continuity','연속성')]:lines.append(f'- {label}: {camera.get(key) or "미작성"}')
            for key,title in [('first_frame_prompt','첫 프레임 프롬프트'),('last_frame_prompt','마지막 프레임 프롬프트'),('motion_prompt','연결 동작·카메라 프롬프트')]:lines+=['',f'#### {title}','',str(detail.get(key) or '미작성 · 정본 및 실제 프레임을 바탕으로 보완해야 합니다.')]
            lines+=['','#### 컷 내부 리듬과 전체 영상 위치','', '| 컷 내부(초) | 전체 영상 구간 | 행동·리듬 |','| --- | --- | --- |']
            for phase in detail.get('timing',[]):lines.append(f'| {phase["start_s"]:.3f}–{phase["end_s"]:.3f} | {timecode(row["start_s"]+phase["start_s"])}–{timecode(row["start_s"]+phase["end_s"])} | {cell(phase["action"])} |')
            if not detail.get('timing'):lines.append('| 미작성 | 미작성 | 컷 내부 리듬 설계 필요 |')
    look=next((a for a in canonical.get('asset_registry',[]) if isinstance(a,dict) and a.get('id')==canonical.get('look_asset_id')),None)
    if look and isinstance(look.get('path'),str):lines+=['','## 룩·씬별 카메라 의도','',f'[등록된 룩·씬 설계 문서]({link(look["path"])})']
    lines+=['','## 설계 출처','', '- 이 문서는 project.json과 검증된 production_handoff.json에서 자동 파생합니다. 창작 정본이나 사용자 수락을 대체하지 않습니다.',f'- 정본 SHA-256: `{project_hash}`', '- 이미지 링크는 이 문서가 저장되는 06_GENERATION_SPECS 폴더 기준 상대 경로입니다.', '- 미완성 표시가 있는 항목은 제작 완료나 생성 성공의 근거가 아닙니다.','']
    return '\n'.join(lines)

def markdown(root):
    # A concurrent agent may revise source files while a download is being prepared.
    paths=[confined(root,'project.json',False),confined(root,'06_GENERATION_SPECS/production_handoff.json',False)]
    def snapshot():
        return [p.read_bytes() if p.is_file() else None for p in paths]
    before=snapshot();text=_markdown(root)
    if before!=snapshot():raise Problem('설계가 갱신 중입니다. 잠시 후 다시 다운로드하세요.',409)
    return text

def write_export(root):
    text=markdown(root);file=confined(root,EXPORT,False);file.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=file.parent,prefix='.storyboard-',suffix='.tmp',delete=False) as output:
            temporary=Path(output.name);output.write(text);output.flush();os.fsync(output.fileno())
        temporary.replace(file)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return {'path':EXPORT,'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),'derived':True}
