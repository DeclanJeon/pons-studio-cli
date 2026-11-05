"""Provider credentials stay here; the agent receives connection IDs only."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from .store import Problem

CATALOG=[
 {'id':'fish','name':'Fish Audio','letter':'F','color':'#e87846','roles':['speech','image'],'transports':['mcp','api'],'mcp_url':'https://api.fish.audio/mcp','description':'캐릭터 대사와 나레이션','auth':'OAuth 또는 API 키'},
 {'id':'elevenlabs','name':'ElevenLabs','letter':'Ⅱ','color':'#252936','roles':['speech','image'],'transports':['mcp','api'],'mcp_url':'https://api.us.elevenlabs.io/v1/mcp','description':'보이스와 이미지 제작','auth':'OAuth 또는 API 키'},
 {'id':'typecast','name':'Typecast','letter':'t','color':'#6954c9','roles':['speech'],'transports':['api'],'description':'한국어 캐릭터와 감정 음성','auth':'API 키'},
 {'id':'custom','name':'다른 MCP / CLI','letter':'+','color':'#47787f','roles':['image','speech','video','research'],'transports':['mcp','cli'],'description':'기존 제작 도구를 연결하세요','auth':'MCP OAuth 또는 설치된 CLI'},
]

def request(url,key,provider,body=None,timeout=30):
    headers={'Accept':'application/json','User-Agent':'PonsStudio/0.1'}
    headers['xi-api-key' if provider=='elevenlabs' else 'X-API-KEY' if provider=='typecast' else 'Authorization']=key if provider!='fish' else 'Bearer '+key
    if body is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            raw=response.read(80*1024*1024+1)
            if len(raw)>80*1024*1024:raise Problem('공급자 결과가 허용 크기를 초과했습니다.')
            return raw,response.headers.get('Content-Type','')
    except urllib.error.HTTPError as e:
        # Do not echo provider bodies or headers; they can contain secrets.
        text={401:'인증 정보가 유효하지 않습니다.',403:'이 기능을 사용할 권한이 없습니다.',402:'공급자 잔액 또는 사용 권한을 확인하세요.',429:'공급자 사용 한도에 도달했습니다.'}.get(e.code,f'공급자 요청이 실패했습니다. HTTP {e.code}')
        raise Problem(text,502)
    except (urllib.error.URLError,TimeoutError):raise Problem('공급자에 연결하지 못했습니다. 잠시 후 연결을 확인하세요.',502)

def voices(connection):
    provider=connection['provider'];key=connection.get('secret','')
    if connection['transport']!='api':return []
    urls={'fish':'https://api.fish.audio/model?page_size=50','elevenlabs':'https://api.elevenlabs.io/v1/voices','typecast':'https://api.typecast.ai/v2/voices'}
    raw,_=request(urls[provider],key,provider)
    data=json.loads(raw)
    if isinstance(data,list):items=data
    else:items=data.get('voices') or data.get('items') or data.get('data') or []
    if not isinstance(items,list):raise Problem('공급자의 보이스 목록 형식이 변경됐습니다.',502)
    return [{'id':str(x.get('voice_id') or x.get('_id') or x.get('id')),
             'name':str(x.get('voice_name') or x.get('name') or x.get('title') or x.get('voice_id') or '보이스'),
             **{k:x[k] for k in ('models','voice_type','gender','language','languages') if k in x}}
            for x in items if isinstance(x,dict) and (x.get('voice_id') or x.get('_id') or x.get('id'))]

def synthesize(connection,text,voice_id,model=None):
    if not isinstance(text,str) or not 1<=len(text)<=2000:raise Problem('발화는 1자 이상 2,000자 이하로 나눠주세요.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',voice_id or ''):raise Problem('보이스 ID를 확인하세요.')
    provider=connection['provider'];key=connection.get('secret','')
    if connection['transport']!='api':raise Problem('이 연결은 Codex의 MCP 또는 CLI 도구를 사용하세요.')
    if provider=='fish':
        # Model is intentionally not inferred; API uses the selected account's default.
        body={'text':text,'reference_id':voice_id,'format':'mp3'}
        raw,mime=request('https://api.fish.audio/v1/tts',key,provider,body,180)
    elif provider=='elevenlabs':
        body={'text':text,'model_id':model or 'eleven_multilingual_v2'}
        raw,mime=request('https://api.elevenlabs.io/v1/text-to-speech/'+voice_id,key,provider,body,180)
    elif provider=='typecast':
        body={'text':text,'voice_id':voice_id,'model':model or 'ssfm-v30'}
        raw,mime=request('https://api.typecast.ai/v1/text-to-speech',key,provider,body,180)
    else:raise Problem('이 공급자의 음성 API는 등록되지 않았습니다.')
    if not raw or ('json' in mime):raise Problem('공급자가 음성 파일을 반환하지 않았습니다.',502)
    return raw,'.wav' if ('wav' in mime or raw[:4]==b'RIFF') else '.mp3'
