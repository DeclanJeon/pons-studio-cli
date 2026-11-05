"""Minimal first-run login and explicit, optional connection configuration."""
from __future__ import annotations

import threading
from typing import assert_never

from server.capabilities import AuthState
from server.store import Problem
from .client import Client
from .render import emit


def login(client: Client, connection: str | None = None) -> None:
    """Guide the backend's supported OAuth flow; cancellation never deletes auth."""
    path = f"/api/connections/{connection}/auth" if connection else "/api/auth/codex"
    result = client.request("POST", path, {})
    identifier = result.get('id') if isinstance(result, dict) else None
    if not isinstance(identifier, str):
        raise Problem("로그인 요청을 확인하지 못했습니다.", 502)
    emit('로그인 대기 중입니다. 주소/코드가 표시되면 인증하세요. Ctrl-C로 이번 로그인만 취소합니다.')
    if connection:
        emit(f'수동 callback이 필요한 공급자는 다른 터미널에서 같은 --data로 auth-reply {identifier} 명령을 실행하세요.')
    shown: set[str] = set()
    wake = threading.Event()
    try:
        while True:
            state = client.request("GET", "/api/auth/" + identifier)
            if not isinstance(state, dict):
                raise Problem("로그인 상태 응답이 올바르지 않습니다.", 502)
            for key in ("url", "code"):
                value = state.get(key)
                if isinstance(value, str) and value and value not in shown:
                    # Transient display only. Never write these into production history.
                    emit(value)
                    shown.add(value)
            if state.get("status") != "pending":
                emit(str(state.get("message", "로그인 종료")))
                return
            wake.wait(0.5)
    except (KeyboardInterrupt, EOFError):
        client.request("POST", f"/api/auth/{identifier}/cancel", {})
        emit("로그인만 취소했습니다. 기존 인증은 보존됩니다.")


def ensure_ready(client: Client) -> AuthState:
    """Valid/unknown existing auth never prompts for full setup."""
    reply = client.request("GET", "/api/cli/readiness")
    if not isinstance(reply, dict):
        raise Problem("인증 상태 응답이 올바르지 않습니다.", 502)
    raw = reply.get("state")
    state: AuthState
    match raw:
        case "authenticated" | "unauthenticated" | "unknown" | "missing":
            state = raw
        case _:
            raise Problem("지원하지 않는 인증 상태입니다.", 502)
    match state:
        case "authenticated":
            emit("기존 인증을 재사용합니다. 설정을 건너뜁니다." + (" API 계정 과금은 별도입니다." if reply.get("method") == "api_key" else ""))
        case "unknown":
            emit("인증 상태를 확인하지 못했습니다. 기존 인증은 보존하며 실행 전에 다시 확인합니다.")
        case "missing":
            emit("Codex CLI가 없습니다. 자동 설치하지 않습니다. 설치 후 pons setup으로 연결하세요.")
        case "unauthenticated":
            if input("Codex 로그인을 시작할까요? [y/N] ").strip().lower() in ("y", "yes"):
                login(client)
        case unreachable:
            assert_never(unreachable)
    return state


def configure(client: Client) -> None:
    """Explicit setup only: inspect, optionally login or register selected tools."""
    ensure_ready(client)
    while True:
        emit("설정: 1) Codex 로그인  2) 기존 MCP 선택  3) MCP 인증  4) CLI/API 연결 등록  5) 외부 스킬 경로 연결  Enter) 돌아가기")
        choice = input("설정> ").strip()
        match choice:
            case "":
                return
            case "1":
                login(client)
            case "2":
                catalog = client.request("GET", "/api/cli/system-catalog")
                emit(str(catalog))
                names = input("가져올 MCP 이름(쉼표 구분, 비밀정보 복사 없음)> ").strip()
                if names:
                    emit(str(client.request("POST", "/api/cli/connect-system", {"names": [n.strip() for n in names.split(",")]})))
            case "3":
                emit(str(client.request("GET", "/api/connections")))
                identifier = input("인증할 연결 ID> ").strip()
                if identifier:
                    login(client, identifier)
            case "4":
                import json
                emit("연결 JSON을 입력하세요. provider/transport/role/name/config, API 연결은 api_key 포함. 외부 CLI 설치·인증은 자동 실행하지 않습니다.")
                # API-key entry is masked to avoid echo/history exposure.
                import getpass
                raw = getpass.getpass("연결 JSON(입력 숨김)> ")
                emit(str(client.request("POST", "/api/connections", json.loads(raw))))
            case "5":
                name = input('스킬 이름(codex-imagen 또는 ai-delegation-loop)> ').strip()
                path = input('기존 SKILL.md 또는 스킬 폴더 경로> ').strip()
                emit(str(client.request('POST', '/api/environment/skill', {'name': name, 'path': path})))
            case _:
                emit("메뉴 번호를 선택하세요.")
