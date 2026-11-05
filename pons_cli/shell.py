"""A thin interactive client; the shared backend owns production and its queue."""
from __future__ import annotations

import json
import shlex
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from server.store import Problem
from .client import Client, JSON
from .render import emit
from .setup import configure


@dataclass
class Shell:
    """Mutable active conversation and watcher; never owns the worker process."""
    client: Client
    workspace: Path
    private_data: Path | None = None
    project: str | None = None
    execution: bool = False
    options: dict[str, JSON] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    watcher: threading.Thread | None = None

    def close(self) -> None:
        self.stop_event.set()
        if self.watcher:
            self.watcher.join(timeout=4)

    def attach(self, identifier: str) -> None:
        """Verify a project before switching, keeping its exact backend session."""
        project = self.client.request('GET', '/api/projects/' + identifier)
        if not isinstance(project, dict):
            raise Problem('프로젝트 응답 형식이 올바르지 않습니다.', 502)
        self.close()
        self.project = identifier
        self.execution = False  # Attaching is not fresh production authority.
        self.attachments.clear()
        self.stop_event = threading.Event()
        self.watcher = threading.Thread(target=self.watch, args=(identifier, self.stop_event), daemon=True)
        self.watcher.start()
        emit('대화 연결: ' + identifier + ' (기존 실행은 그대로 유지됩니다.)')

    def watch(self, identifier: str, stopped: threading.Event) -> None:
        """Replay event sequence and current status; never retry a submission."""
        client = Client(self.client.url, self.client.token, timeout=2)
        after = 0
        previous = ''
        while not stopped.is_set():
            try:
                events = client.request('GET', f'/api/projects/{identifier}/event-log?after={after}')
                if isinstance(events, list):
                    for event in events:
                        if isinstance(event, dict) and isinstance(event.get('seq'), int):
                            after = event['seq']
                            emit(f'\n[{event.get("kind", "event")}] {event.get("message", "")}')
                project = client.request('GET', '/api/projects/' + identifier)
                if isinstance(project, dict):
                    status = str(project.get('status', ''))
                    if status != previous:
                        previous = status
                        emit('\n상태: ' + status)
                        if project.get('error'):
                            emit(str(project['error']))
                        if project.get('result'):
                            emit(json.dumps(project['result'], ensure_ascii=False))
                stopped.wait(1)
            except Problem as error:
                emit('\n모니터 연결 중단: ' + error.message + ' /attach로 다시 연결하세요. 작업을 재제출하지 않습니다.')
                return

    def allow(self) -> None:
        """Explicit per-turn consent, without claiming an enforceable monetary cap."""
        emit('선택한 도구의 생성 실행을 이 요청에 허용합니다. MCP·CLI 비용은 공급자 계정에 청구될 수 있으며 앱은 금액 상한을 보장하지 않습니다. 생성형 영상은 별도 선택·정확한 견적 승인이 필요합니다.')
        self.execution = input('이 요청의 실행과 해당 계정 과금 범위에 동의하나요? [y/N] ').strip().lower() in ('y', 'yes')

    def send(self, message: str) -> None:
        """Submit exactly once with an idempotent message ID; never shell-evaluate."""
        payload: dict[str, JSON] = {'message': message, 'execution_authorized': self.execution,
                                    'inputs': list(self.attachments), 'workflow_enabled': True}
        if self.project:
            path = f'/api/projects/{self.project}/messages'
            payload['client_message_id'] = str(uuid.uuid4())
            self.client.request('POST', path, payload)
        else:
            payload['workspace'] = str(self.workspace)
            payload['options'] = self.options
            created = self.client.request('POST', '/api/agent/projects', payload, {'Idempotency-Key': str(uuid.uuid4())})
            identifier = created.get('id') if isinstance(created, dict) else None
            if not isinstance(identifier, str):
                raise Problem('프로젝트 생성 응답을 확인하지 못했습니다.', 502)
            self.attach(identifier)
        self.attachments.clear()
        self.execution = False

    def command(self, line: str) -> bool:
        """Return false only for explicit detach/exit; commands never stop a worker."""
        parts = shlex.split(line)
        name, *args = parts
        match name:
            case '/exit' | '/quit':
                return False
            case '/help':
                emit('/run <제작 지시> 실행 동의 후 제작 | /attach <ID> | /status | /projects | /stop | /resume | /settings | /use <speech|image> <연결ID> | /plan <질문> | /attach-file <파일> | /video <연결ID> | /approve-video <견적ID> | /exit')
                emit('일반 입력은 실행 동의를 확인합니다. 거절하면 계획·조회만 합니다. /plan은 질문 전용입니다. 게시 승인은 포함하지 않습니다.')
            case '/settings':
                configure(self.client)
            case '/projects':
                emit(json.dumps(self.client.request('GET', '/api/projects'), ensure_ascii=False, indent=2))
            case '/attach':
                if len(args) != 1:
                    raise Problem('/attach <프로젝트ID>')
                self.attach(args[0])
            case '/use':
                if self.project:
                    raise Problem('연결 선택은 새 대화를 시작하기 전에 설정하세요. 기존 프로젝트의 공급자를 몰래 바꾸지 않습니다.')
                if len(args) != 2 or args[0] not in ('speech', 'image'):
                    raise Problem('/use <speech|image> <연결ID>')
                connections = self.options.get('connections', {})
                if not isinstance(connections, dict):
                    connections = {}
                self.options['connections'] = {**connections, args[0]: args[1]}
                emit('다음 프로젝트의 연결 선택을 반영했습니다.')
            case '/plan':
                if not args:
                    raise Problem('/plan <질문>')
                self.execution = False
                self.send(line.partition(' ')[2])
            case '/run':
                if not args:
                    raise Problem('/run <영상 제작 지시>')
                self.allow()
                if self.execution:
                    self.send(line.partition(' ')[2])
            case '/status' | '/stop' | '/resume' | '/video' | '/approve-video':
                if not self.project:
                    raise Problem('먼저 대화를 시작하거나 /attach 하세요.')
                match name:
                    case '/status':
                        try:
                            emit(json.dumps(self.client.request('GET', f'/api/projects/{self.project}/workflow'), ensure_ascii=False, indent=2))
                        except Problem as error:
                            if error.status != 404:
                                raise
                        emit(json.dumps(self.client.request('GET', '/api/projects/' + self.project), ensure_ascii=False, indent=2))
                        emit(json.dumps(self.client.request('GET', f'/api/projects/{self.project}/video'), ensure_ascii=False, indent=2))
                    case '/stop':
                        emit(str(self.client.request('POST', f'/api/projects/{self.project}/cancel', {})))
                    case '/resume':
                        if input('원격 작업 상태를 확인했나요? 확인 전에는 재개하지 마세요. [y/N] ').lower() in ('y', 'yes'):
                            emit(str(self.client.request('POST', f'/api/projects/{self.project}/resume', {'remote_checked': True})))
                    case '/video':
                        if len(args) != 1:
                            raise Problem('/video <영상 연결ID>')
                        emit(str(self.client.request('POST', f'/api/projects/{self.project}/video-settings', {'connection_id': args[0], 'motion_video_authorized': True})))
                    case '/approve-video':
                        if len(args) != 1:
                            raise Problem('/approve-video <견적ID>')
                        emit(str(self.client.request('GET', f'/api/projects/{self.project}/video')))
                        if input('표시된 정확한 견적을 승인하나요? [y/N] ').lower() in ('y', 'yes'):
                            emit(str(self.client.request('POST', f'/api/projects/{self.project}/video-approval', {'quote_id': args[0]})))
            case '/attach-file':
                if len(args) != 1:
                    raise Problem('/attach-file <파일>')
                result = self.client.upload(Path(args[0]).expanduser())
                identifier = result.get('id') if isinstance(result, dict) else None
                if isinstance(identifier, str):
                    self.attachments.append(identifier)
                    emit('다음 메시지에 첨부합니다: ' + args[0])
            case _:
                raise Problem('알 수 없는 명령입니다. /help를 확인하세요.')
        return True

    def run(self) -> int:
        emit('Pons Studio · 작업 폴더: ' + str(self.workspace))
        emit('만들고 싶은 영상을 이야기하세요. /run <지시>로 이번 제작을 허용합니다. /help 도움말, /exit 연결 종료.')
        try:
            while True:
                try:
                    line = input('pons> ').strip()
                    if not line:
                        continue
                    if line.startswith('/'):
                        if not self.command(line):
                            return 0
                    else:
                        self.allow()
                        self.send(line)
                except KeyboardInterrupt:
                    emit('\n입력을 취소했습니다. 실행 중인 제작은 유지합니다. /stop은 별도입니다.')
                except (Problem, ValueError, OSError) as error:
                    emit(error.message if isinstance(error, Problem) else str(error))
        except EOFError:
            return 0
        finally:
            self.close()
            prefix = 'pons' + (f' --data {shlex.quote(str(self.private_data))}' if self.private_data else '')
            emit('연결을 종료했습니다. 실행 중인 작업은 유지됩니다.' + (f' {prefix} attach {self.project}' if self.project else ''))
