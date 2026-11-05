"""Codex stdio adapter with exact-session resume and no automatic resubmission."""
import json
import queue
import subprocess
import threading
import time


class AppServerProcess:
    """Popen-compatible lifecycle; normalized events feed the existing runner."""
    def __init__(self,args,env,cwd,prompt,session_id=None,authorized=False):
        self.process=subprocess.Popen(args,env=env,cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,text=True,bufsize=1,start_new_session=True)
        self.pid=self.process.pid
        self.lines=queue.Queue();self.incoming=queue.Queue();self.done=threading.Event()
        self.code=None;self.sequence=0;self.cwd=cwd;self.prompt=prompt
        self.session_id=session_id;self.authorized=authorized
        self.thread_id=None;self.turn_id=None
        self.terminal=None
        self.write_lock=threading.Lock()
        threading.Thread(target=self._read,daemon=True).start()
        threading.Thread(target=self._stderr,daemon=True).start()
        threading.Thread(target=self._serve,daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:self.incoming.put(json.loads(line))
                except ValueError:continue
        finally:self.process.stdout.close();self.incoming.put(None)

    def _stderr(self):
        try:
            for line in self.process.stderr:
                if 'error' in line.lower():self.lines.put(('err',line))
        finally:self.process.stderr.close()

    def send(self,value):
        with self.write_lock:
            self.process.stdin.write(json.dumps(value,ensure_ascii=False)+'\n');self.process.stdin.flush()

    def interrupt_and_wait(self,timeout=8):
        if self.done.is_set():return True
        if not self.thread_id or not self.turn_id:return False
        try:self.send({'id':'pons-interrupt','method':'turn/interrupt','params':{'threadId':self.thread_id,'turnId':self.turn_id}})
        except (OSError,ValueError):return False
        return self.done.wait(timeout)

    def emit(self,value):self.lines.put(('out',json.dumps(value,ensure_ascii=False)))

    def request(self,method,params,timeout=60):
        self.sequence+=1;identifier=self.sequence
        self.send({'id':identifier,'method':method,'params':params})
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:message=self.incoming.get(timeout=min(1,max(.01,deadline-time.monotonic())))
            except queue.Empty:continue
            if message is None:raise RuntimeError('Codex app-server 연결이 종료됐습니다.')
            if message.get('id')==identifier and 'method' not in message:
                if message.get('error'):raise RuntimeError(message['error'].get('message','Codex 요청 실패'))
                return message.get('result',{})
            result=self.notification(message)
            if result is not None:self.terminal=result
        raise RuntimeError('Codex app-server 응답 시간이 초과됐습니다. 기존 세션을 보존했습니다.')

    def notification(self,message):
        method=message.get('method','');params=message.get('params') or {}
        if 'id' in message and method:
            # Never silently grant a new approval, elicitation or permission.
            self.send({'id':message['id'],'error':{'code':-32601,'message':'Pons requires this permission to be configured before execution.'}})
            raise RuntimeError('추가 인증 또는 권한이 필요합니다. 도구 연결과 실행 범위를 확인하세요.')
        if method in ('item/started','item/completed'):
            item=params.get('item',{});kind=item.get('type')
            if kind=='agentMessage' and method=='item/completed':
                self.emit({'type':'item.completed','item':{'type':'agent_message','text':item.get('text','')}})
            elif kind=='mcpToolCall':
                self.emit({'type':method.replace('/','.'),'item':{'type':'mcp_tool_call',
                           'server':item.get('server'),'tool':item.get('tool'),'status':item.get('status')}})
        if method=='error':
            error=params.get('error',{})
            self.emit({'type':'error','error':error})
        if method=='turn/completed':
            turn=params.get('turn',{})
            # Ignore events belonging to any other thread/turn.
            if params.get('threadId')!=self.thread_id:return None
            if self.turn_id and turn.get('id')!=self.turn_id:return None
            if turn.get('status')!='completed':
                error=turn.get('error') or {'message':'Codex 작업이 완료되지 않았습니다: '+str(turn.get('status'))}
                self.emit({'type':'turn.failed','error':error});return 1
            return 0
        return None

    def _serve(self):
        code=1
        try:
            self.request('initialize',{'clientInfo':{'name':'pons_studio','title':'Pons Studio','version':'0.2.0'}})
            self.send({'method':'initialized'})
            params={'cwd':self.cwd,'approvalPolicy':'never','sandbox':'workspace-write' if self.authorized else 'read-only'}
            if self.session_id:params['threadId']=self.session_id
            response=self.request('thread/resume' if self.session_id else 'thread/start',params)
            self.thread_id=response['thread']['id']
            if self.session_id and self.thread_id!=self.session_id:raise RuntimeError('Codex가 다른 세션을 반환했습니다. 원래 세션을 보존합니다.')
            self.emit({'type':'thread.started','thread_id':self.thread_id})
            policy={'type':'workspaceWrite','writableRoots':[self.cwd],'networkAccess':True} if self.authorized else {'type':'readOnly'}
            response=self.request('turn/start',{'threadId':self.thread_id,'input':[{'type':'text','text':self.prompt}],
                'cwd':self.cwd,'approvalPolicy':'never','sandboxPolicy':policy})
            self.turn_id=response['turn']['id']
            if self.terminal is not None:code=self.terminal;return
            while True:
                message=self.incoming.get()
                if message is None:raise RuntimeError('Codex app-server 연결이 끊겼습니다. 같은 세션에서 다시 확인하세요.')
                result=self.notification(message)
                if result is not None:code=result;break
        except Exception as error:
            self.emit({'type':'turn.failed','error':{'message':str(error)}})
        finally:
            try:self.process.stdin.close()
            except OSError:pass
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            self.code=code;self.done.set()
            self.lines.put(('closed','out'));self.lines.put(('closed','err'))

    def poll(self):return self.code if self.done.is_set() else None

    def wait(self,timeout=None):
        if not self.done.wait(timeout):raise subprocess.TimeoutExpired('codex app-server',timeout)
        return self.code
