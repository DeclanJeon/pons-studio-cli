"""Bounded HTTP transport to a same-user loopback backend; never redirects."""
from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from urllib.parse import quote, urlsplit

from server.store import Problem

JSON: TypeAlias = str | int | float | bool | None | list["JSON"] | dict[str, "JSON"]


@dataclass(frozen=True, slots=True)
class Client:
    url: str
    token: str = ""
    timeout: float = 15

    def request(self, method: str, path: str, payload: JSON = None,
                headers: dict[str, str] | None = None, *, raw_body: bytes | None = None) -> JSON:
        """Decode one bounded JSON response; errors retain HTTP status."""
        target = urlsplit(self.url)
        if target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port or target.username or target.password or target.path or target.query or target.fragment:
            raise Problem("로컬 Pons backend 주소만 사용할 수 있습니다.", 403)
        if not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise Problem("허용되지 않은 API 경로입니다.")
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=self.timeout)
        request_headers = {"Content-Type": "application/json", "X-Pons-Session": self.token, **(headers or {})}
        body = raw_body if raw_body is not None else json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise Problem("backend 응답이 너무 큽니다.", 502)
            decoded: JSON = json.loads(raw)
            if response.status >= 300:
                message = decoded.get("error", "backend 요청 실패") if isinstance(decoded, dict) else "backend 요청 실패"
                raise Problem(str(message), response.status)
            return decoded
        except (OSError, http.client.HTTPException) as error:
            raise Problem("backend 연결이 끊겼습니다. 저장된 작업에 다시 연결하세요.", 503) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise Problem("backend JSON 응답을 읽을 수 없습니다.", 502) from error
        finally:
            connection.close()

    def upload(self, path: Path) -> JSON:
        """Upload a bounded local attachment through the existing backend boundary."""
        with path.open('rb') as stream:
            content = stream.read(100 * 1024 * 1024 + 1)
        if not content or len(content) > 100 * 1024 * 1024:
            raise Problem('첨부 파일은 1바이트 이상 100 MB 이하여야 합니다.')
        return self.request('POST', '/api/inputs', headers={'Content-Type': 'application/octet-stream', 'X-File-Name': quote(path.name)}, raw_body=content)

    def authenticate(self, endpoint_token: str) -> Client:
        """Exchange trusted endpoint material for an expiring in-memory session."""
        reply = self.request("POST", "/api/cli/session", {}, {"X-Pons-Endpoint": endpoint_token})
        token = reply.get('token') if isinstance(reply, dict) else None
        if not isinstance(token, str):
            raise Problem("CLI 세션 응답을 확인할 수 없습니다.", 502)
        return Client(self.url, token, self.timeout)
