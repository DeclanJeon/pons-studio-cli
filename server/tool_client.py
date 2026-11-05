"""Job-scoped CLI called by Codex. Credentials never enter its JSON payload."""
import json,os,sys,urllib.request,urllib.error
from pathlib import Path

def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('progress','voices','tts','render','block','configure','agent_finish','video','workflow'):raise SystemExit('usage: tool_client.py progress|voices|tts|render|block|configure|agent_finish|video|workflow < payload.json')
    payload=json.load(sys.stdin)
    control={}
    for parent in [Path.cwd(),*Path.cwd().parents]:
        candidate=parent/'.studio-control.json'
        if candidate.is_file():control=json.loads(candidate.read_text());break
    base=os.environ.get('STUDIO_API',control.get('api',''))
    job_id=os.environ.get('STUDIO_JOB_ID',control.get('job_id',''))
    token=os.environ.get('STUDIO_JOB_TOKEN',control.get('token',''))
    if not base or not job_id or not token:raise SystemExit('활성 제작 작업의 실행 정보가 없습니다.')
    url=base+'/api/internal/'+job_id+'/'+sys.argv[1]
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    try:
        with urllib.request.urlopen(req,timeout=14500) as response:print(response.read().decode())
    except urllib.error.HTTPError as e:
        print(e.read().decode());raise SystemExit(1)

if __name__=='__main__':main()
