"""Deterministic still-image animatic assembly; never manufactures speech or imagery."""
import hashlib
import json
import math
import shutil
import subprocess
import time
import tempfile
from pathlib import Path
from PIL import Image
from .store import Problem, atomic_json, confined

def probe(path):
    fmt={'.wav':'wav','.mp3':'mp3','.m4a':'mov','.mp4':'mov','.mov':'mov','.flac':'flac','.ogg':'ogg'}.get(Path(path).suffix.lower())
    if not fmt:raise Problem('지원하는 오디오 또는 영상 파일이 필요합니다.')
    proc=subprocess.run(['ffprobe','-v','error','-f',fmt,'-show_format','-show_streams','-of','json',str(path)],capture_output=True,text=True,timeout=30)
    if proc.returncode:raise Problem('미디어 파일을 읽을 수 없습니다. 파일 형식을 확인하세요.')
    return json.loads(proc.stdout)

def duration(path):
    info=probe(path)
    try:value=float(info['format']['duration'])
    except (KeyError,ValueError):raise Problem('음성의 실제 길이를 측정할 수 없습니다.')
    if not math.isfinite(value) or value<=0:raise Problem('음성 길이가 올바르지 않습니다.')
    return value

def srt_time(value):
    ms=round(value*1000);s,ms=divmod(ms,1000);m,s=divmod(s,60);h,m=divmod(m,60)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'

def validate_timeline(root,data):
    if data.get('fps') not in (24,25,30):raise Problem('타임라인 fps는 24, 25, 30 중 하나여야 합니다.')
    fps=data['fps'];shots=data.get('shots',[]);audio=data.get('audio',[])
    if not shots or len(shots)>300:raise Problem('타임라인에 1개 이상 300개 이하의 컷이 필요합니다.')
    if not audio or len(audio)>300:raise Problem('실제 음성 트랙이 필요합니다.')
    end=0;ids=set()
    for shot in shots:
        if not isinstance(shot.get('start_frame'),int) or not isinstance(shot.get('end_frame'),int):raise Problem('컷의 시작과 종료는 프레임 정수여야 합니다.')
        if shot['start_frame']!=end or shot['end_frame']<=end:raise Problem('컷 사이에 빈 구간 또는 겹침이 있습니다.')
        if not shot.get('id') or shot['id'] in ids:raise Problem('컷 ID는 비어 있지 않고 고유해야 합니다.')
        ids.add(shot['id']);picture=confined(root,shot['image'])
        try:
            with Image.open(picture) as image:
                if image.format not in ('PNG','JPEG','WEBP'):raise Problem('PNG, JPEG 또는 WebP 컷 이미지가 필요합니다.')
                image.verify()
        except (OSError,ValueError):raise Problem('컷 이미지 파일을 읽을 수 없습니다.')
        end=shot['end_frame']
    total=end/fps
    if total>3600:raise Problem('애니매틱 길이는 1시간 이내여야 합니다.')
    speech_ids=set()
    for item in audio:
        if not item.get('id') or item['id'] in speech_ids:raise Problem('발화 ID는 고유해야 합니다.')
        speech_ids.add(item['id'])
        start=float(item.get('start_s',0))
        actual=duration(confined(root,item['file']))
        if start<0 or start+actual>total+1/fps+.03:raise Problem('음성이 타임라인 범위를 벗어납니다.')
        item['measured_duration_s']=actual
    for c in data.get('captions',[]):
        if not 0<=float(c['start_s'])<float(c['end_s'])<=total+.001:raise Problem('자막 시간이 타임라인 범위를 벗어납니다.')
        if len(c.get('text',''))>240:raise Problem('자막 한 구간이 너무 깁니다. 문장 단위로 나누세요.')
    return total

def run_command(args,cancelled,deadline):
    # A file avoids a full stderr pipe deadlocking a failed encoder.
    with tempfile.TemporaryFile() as error_file:
        proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=error_file)
        while proc.poll() is None:
            if cancelled() or time.monotonic()>deadline:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                raise Problem('렌더링이 중지되었거나 실행 시간을 초과했습니다.',409)
            time.sleep(.15)
        error_file.seek(max(0,error_file.tell()-1000));error=error_file.read().decode(errors='replace')
        if proc.returncode:raise Problem('영상 렌더링에 실패했습니다. 입력 파일과 FFmpeg 구성을 확인하세요. '+error[-500:])

def render(root,timeline,aspect='16:9',cancelled=lambda:False,timeout=1800,size=None):
    root=Path(root);data=json.loads(json.dumps(timeline));total=validate_timeline(root,data)
    width,height=size or ((1920,1080) if aspect=='16:9' else (1080,1920))
    fps=data['fps'];out=root/'10_DELIVERY';out.mkdir(parents=True,exist_ok=True)
    # Every invocation gets its own directory so rerenders cannot mix old clips.
    work=root/'08_ANIMATIC_EDIT'/f'render_{time.time_ns()}';work.mkdir(parents=True)
    deadline=time.monotonic()+timeout
    clips=[]
    for i,shot in enumerate(data['shots']):
        image=confined(root,shot['image']);clip=work/f'{i:04}.mp4'
        frames=shot['end_frame']-shot['start_frame']
        vf=f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p'
        run_command(['ffmpeg','-hide_banner','-loglevel','error','-y','-loop','1','-framerate',str(fps),'-i',str(image),'-vf',vf,'-frames:v',str(frames),'-an','-c:v','libx264','-preset','veryfast','-crf','21','-threads','2',str(clip)],cancelled,deadline)
        clips.append(clip)
    listing=work/'clips.txt';listing.write_text(''.join(f"file '{p.name}'\n" for p in clips))
    args=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing)]
    filters=[];mix=[]
    for i,item in enumerate(data['audio'],1):
        args+=['-i',str(confined(root,item['file']))]
        delay=round(float(item.get('start_s',0))*1000)
        filters.append(f'[{i}:a]aresample=48000,adelay={delay}:all=1[a{i}]');mix.append(f'[a{i}]')
    filters.append(''.join(mix)+f'amix=inputs={len(mix)}:normalize=0,apad,atrim=duration={total}[sound]')
    base=work/'base.mp4'
    args+=['-filter_complex',';'.join(filters),'-map','0:v','-map','[sound]','-t',str(total),'-c:v','copy','-c:a','aac','-ar','48000','-movflags','+faststart',str(base)]
    run_command(args,cancelled,deadline)
    version=f'{time.time_ns()}';video=out/f'animatic_{version}.mp4';subtitle=out/f'captions_{version}.srt'
    subtitle.write_text('\n\n'.join(f"{i}\n{srt_time(c['start_s'])} --> {srt_time(c['end_s'])}\n{c['text']}" for i,c in enumerate(data.get('captions',[]),1)),encoding='utf-8')
    if data.get('captions'):
        # File lives in our generated directory; escape libavfilter syntax, not a shell.
        escaped=str(subtitle).replace('\\','\\\\').replace(':','\\:').replace("'","\\'")
        run_command(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(base),'-vf',f"subtitles=filename='{escaped}':force_style='FontName=Noto Sans CJK KR,FontSize=20,Outline=1,MarginV=30'",'-c:v','libx264','-preset','veryfast','-crf','21','-threads','2','-c:a','copy','-movflags','+faststart',str(video)],cancelled,deadline)
    else:shutil.copyfile(base,video)
    run_command(['ffmpeg','-v','error','-i',str(video),'-f','null','-'],cancelled,deadline)
    info=probe(video);actual=float(info['format']['duration'])
    if abs(actual-total)>.2:raise Problem('렌더링 길이가 타임라인과 다릅니다.')
    result={'video':str(video.relative_to(root)),'captions':str(subtitle.relative_to(root)),'duration_s':actual,'fps':fps,'sha256':hashlib.sha256(video.read_bytes()).hexdigest(),'technical_checks':'pass','semantic_review':'pending','timeline':data}
    atomic_json(out/'render_result.json',result)
    return result
