"""Discover or start one detached backend for the selected private data root."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from server.store import Problem
from .client import Client

APP_ROOT: Final = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Endpoint:
    url: str
    token: str
    data_id: str


def read_endpoint(data: Path) -> Endpoint:
    """Read exactly one private regular endpoint file without following links."""
    path = data / "desktop-endpoint.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise Problem("backend 연결 파일의 심볼릭 링크는 허용되지 않습니다.", 403) from error
        raise
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384:
            raise Problem("backend 연결 파일의 소유권/권한을 확인하세요.", 403)
        record = json.load(stream)
    identity = hashlib.sha256(str(data.resolve()).encode()).hexdigest()
    if not isinstance(record, dict) or record.get("data_id") != identity or record.get("protocol", 1) != 1:
        raise Problem("다른 데이터 또는 지원하지 않는 backend입니다.", 409)
    url, token = record.get("url"), record.get("token")
    if not isinstance(url, str) or not isinstance(token, str) or not token:
        raise Problem("backend 연결 파일 형식이 올바르지 않습니다.", 409)
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise Problem("로컬 backend 주소만 신뢰할 수 있습니다.", 403)
    return Endpoint(url, token, identity)


def connect(data: Path, start: bool = True) -> Client:
    """Attach to a matching live owner, or race safely through its instance lock."""
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data.stat().st_uid != os.getuid() or stat.S_IMODE(data.stat().st_mode) & 0o077:
        raise Problem("개인 데이터 폴더의 소유권/권한을 확인하세요(0700).", 403)
    def attach() -> Client:
        endpoint = read_endpoint(data)
        client = Client(endpoint.url)
        health = client.request("GET", "/api/desktop/health", headers={"X-Pons-Desktop": endpoint.token})
        if not isinstance(health, dict) or health.get("data_id") != endpoint.data_id or health.get("protocol") != 1:
            raise Problem("backend 데이터/프로토콜이 일치하지 않습니다.", 409)
        if health.get('cli_protocol') != 1:
            raise Problem('실행 중인 구버전 backend는 CLI 연결을 지원하지 않습니다. 제작을 마치고 데스크톱을 종료한 뒤 업데이트된 backend를 실행하세요. 두 번째 worker는 시작하지 않습니다.', 409)
        return client.authenticate(endpoint.token)
    try:
        return attach()
    except FileNotFoundError:
        if not start:
            raise Problem("실행 중인 backend가 없습니다.", 409) from None
    except Problem as error:
        if error.status != 503 or not start:
            raise
    log = data / "cli-backend.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as output:
        process = subprocess.Popen([sys.executable, "-m", "server.app", "--port", "0", "--data", str(data)],
                                   cwd=APP_ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True)
    # Readiness is a state condition. The bounded wait never retries production calls.
    deadline = time.monotonic() + 12
    wake = threading.Event()
    while time.monotonic() < deadline:
        try:
            return attach()
        except FileNotFoundError:
            pass
        except Problem as error:
            if error.status != 503:
                raise
        if process.poll() is not None and process.returncode == 0:
            break
        wake.wait(0.1)
    raise Problem("backend 시작을 확인하지 못했습니다. cli-backend.log와 instance lock을 확인하세요.", 503)
