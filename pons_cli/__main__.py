"""Pons terminal entry point; no installs, logins or paid calls at import time."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from server.store import Problem
from .backend import APP_ROOT, connect
from .paths import data_directory, workspace_directory
from .render import emit
from .setup import configure, ensure_ready
from .shell import Shell


def parser() -> argparse.ArgumentParser:
    """Permit common flags before or after the subcommand without clobbering them."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--data', default=argparse.SUPPRESS, help='개인 DB·인증 저장소 (작업 폴더와 분리)')
    common.add_argument('--workspace', default=argparse.SUPPRESS, help='제작 자료·산출물 폴더')
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='비대화형 결과 JSON')
    root = argparse.ArgumentParser(prog='pons', description='Pons Studio 대화형 영상 제작실', parents=[common])
    commands = root.add_subparsers(dest='command')
    for name in ('chat', 'setup', 'doctor'):
        commands.add_parser(name, parents=[common])
    projects = commands.add_parser('projects', parents=[common])
    projects.add_argument('action', choices=['list'])
    for name in ('status', 'attach', 'resume', 'stop'):
        child = commands.add_parser(name, parents=[common])
        child.add_argument('project')
        if name == 'resume':
            child.add_argument('--remote-checked', action='store_true', help='원격 제출 상태를 확인한 경우에만 재개')
    reply = commands.add_parser('auth-reply', parents=[common])
    reply.add_argument('auth_id')
    reply.add_argument('--callback-stdin', action='store_true', help='인증 callback URL을 argv 대신 stdin으로 전달')
    make = commands.add_parser('make', parents=[common])
    make.add_argument('message')
    make.add_argument('--execute', action='store_true', help='이번 요청의 제작 실행 허용')
    make.add_argument('--allow-provider-charges', action='store_true', help='선택한 공급자 계정 과금 가능성에 동의 (금액 상한 보장 없음)')
    return root


def run(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    as_json = getattr(args, 'json', False)
    data = data_directory(getattr(args, 'data', None), APP_ROOT)
    if args.command == 'doctor':
        dependencies = {name: importlib.util.find_spec(name) is not None for name in ('cryptography', 'PIL', 'tomli_w')}
        report = {'python': sys.version.split()[0], 'data': str(data),
                  'tools': {name: bool(shutil.which(name)) for name in ('codex', 'ffmpeg', 'ffprobe')},
                  'dependencies': dependencies}
        emit(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(dependencies.values()) else 3
    if not importlib.util.find_spec('tomli_w'):
        raise Problem('backend 의존성 tomli-w가 없습니다. 기존 실행 환경 또는 SETUP_GUIDE의 설치 절차를 사용하세요. 자동 설치하지 않습니다.', 409)
    if args.command in (None, 'chat', 'make'):
        raw = getattr(args, 'workspace', None)
        if not raw:
            if as_json or not sys.stdin.isatty():
                raise Problem('--workspace <작업폴더>를 지정하세요.', 409)
            raw = input('작업 폴더> ').strip()
            if not raw:
                raise Problem('작업 폴더를 입력하세요.')
        workspace = workspace_directory(raw, data)
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = Path.home()
    client = connect(data)
    match args.command:
        case 'doctor':
            return 0
        case 'auth-reply':
            import getpass
            callback = sys.stdin.readline().strip() if args.callback_stdin else getpass.getpass('인증 후 callback URL(입력 숨김)> ')
            result = client.request('POST', f'/api/auth/{args.auth_id}/reply', {'callback': callback})
            emit(json.dumps({'status': result.get('status') if isinstance(result, dict) else 'unknown'}, ensure_ascii=False))
            return 0
        case 'setup':
            if as_json or not sys.stdin.isatty():
                raise Problem('설정은 대화형 터미널에서 실행하세요.', 409)
            configure(client)
            return 0
        case 'projects':
            emit(json.dumps(client.request('GET', '/api/projects'), ensure_ascii=False, indent=2))
            return 0
        case 'status':
            emit(json.dumps(client.request('GET', '/api/projects/' + args.project), ensure_ascii=False, indent=2))
            return 0
        case 'stop':
            emit(json.dumps(client.request('POST', f'/api/projects/{args.project}/cancel', {}), ensure_ascii=False))
            return 0
        case 'resume':
            if not args.remote_checked:
                raise Problem('원격 제출 상태를 확인한 뒤 --remote-checked로 재개하세요. 자동 재제출하지 않습니다.', 409)
            emit(json.dumps(client.request('POST', f'/api/projects/{args.project}/resume', {'remote_checked': True}), ensure_ascii=False))
            return 0
        case 'make':
            if not args.execute or not args.allow_provider_charges:
                raise Problem('제작 실행에는 --execute --allow-provider-charges가 필요합니다. MCP/CLI 금액 상한은 보장되지 않습니다.', 409)
            import uuid
            payload = {'message': args.message, 'workspace': str(workspace), 'execution_authorized': True, 'workflow_enabled': True}
            result = client.request('POST', '/api/agent/projects', payload, {'Idempotency-Key': str(uuid.uuid4())})
            emit(json.dumps(result, ensure_ascii=False, indent=2))
            return 0  # Accepted/queued is not delivery completed.
        case 'attach':
            if as_json or not sys.stdin.isatty():
                raise Problem('attach는 대화형 터미널을 사용하세요. 상태 조회는 status --json입니다.', 409)
            shell = Shell(client, workspace, private_data=data)
            shell.attach(args.project)
            return shell.run()
        case None | 'chat':
            if as_json or not sys.stdin.isatty():
                raise Problem('대화는 터미널에서 실행하세요. 비대화형 제작은 make를 사용하세요.', 409)
            ensure_ready(client)
            return Shell(client, workspace, private_data=data).run()
        case _:
            raise Problem('알 수 없는 명령입니다.')


def main(argv: list[str] | None = None) -> int:
    """One CLI error boundary; do not retry authentication or production requests."""
    try:
        return run(argv)
    except Problem as error:
        if '--json' in (argv if argv is not None else sys.argv[1:]):
            emit(json.dumps({'error': error.message, 'http_status': error.status}, ensure_ascii=False))
        else:
            emit('오류: ' + error.message)
        return 3 if error.status in (401, 403, 409, 503) else 1
    except (OSError, ValueError) as error:
        if '--json' in (argv if argv is not None else sys.argv[1:]):
            emit(json.dumps({'error': str(error)}, ensure_ascii=False))
        else:
            emit('오류: ' + str(error))
        return 2
    except (KeyboardInterrupt, EOFError):
        emit('\n연결을 종료합니다. 기존 작업과 인증은 보존됩니다.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
