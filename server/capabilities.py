"""Non-generating authentication discovery; never import credential files."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal, TypedDict

from . import system_integration as system
from .store import Problem, Store, atomic_json

AuthState = Literal["authenticated", "unauthenticated", "unknown", "missing"]


@dataclass(frozen=True, slots=True)
class Authentication:
    state: AuthState
    method: str
    installed: bool


class ImportedConnection(TypedDict):
    id: str
    name: str
    role: str


def authentication(store: Store) -> Authentication:
    """Probe only login status, using existing accounts and a five-second bound."""
    command = shutil.which("codex")
    if not command:
        return Authentication("missing", "unknown", False)
    settings = system.runtime(store)
    mode = settings.get("codex_mode")
    local = store.root / "codex"
    home = system.system_home(store)
    if mode != "system" and ((local / "auth.json").exists() or (local / "config.toml").exists()):
        home = local
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")}
    env["CODEX_HOME"] = str(home)
    try:
        result = subprocess.run([command, "login", "status"], env=env, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return Authentication("unknown", "unknown", True)
    detail = (result.stdout + result.stderr).lower()
    if result.returncode == 0:
        # Only our app metadata changes; original Codex configuration/auth stay in place.
        if home != local:
            atomic_json(store.root / "runtime.json", {**settings, "codex_mode": "system", "system_codex_home": str(home)})
            (store.root / "runtime.json").chmod(0o600)
        method = "chatgpt" if "chatgpt" in detail else "api_key" if "api key" in detail or "api_key" in detail else "unknown"
        return Authentication("authenticated", method, True)
    if "not logged" in detail or "not authenticated" in detail:
        if home != local and mode is None:
            atomic_json(store.root / "runtime.json", {**settings, "codex_mode": "system", "system_codex_home": str(home)})
            (store.root / "runtime.json").chmod(0o600)
        return Authentication("unauthenticated", "unknown", True)
    return Authentication("unknown", "unknown", True)


def select_system_connections(store: Store, names: list[str]) -> list[ImportedConnection]:
    """Register explicitly selected MCP references; never read API-key files."""
    config = system.system_config(store)
    servers = config.get("mcp_servers", {})
    if not names or any(name not in system.PROVIDERS or name not in servers or servers[name].get("enabled") is False for name in names):
        raise Problem("설정된 지원 MCP 이름만 선택하세요.")
    settings = system.runtime(store)
    atomic_json(store.root / "runtime.json", {**settings, "codex_mode": "system", "system_codex_home": str(system.system_home(store))})
    (store.root / "runtime.json").chmod(0o600)
    result: list[ImportedConnection] = []
    for name in dict.fromkeys(names):
        provider, label, roles = system.PROVIDERS[name]
        for role in roles:
            connection = system.upsert(store, "system-mcp:" + name + ":" + role, name=label + " · " + role,
                                       provider=provider, transport="system_mcp", role=role,
                                       config={"system_mcp_name": name, "source": "system"})
            result.append({"id": connection["id"], "name": connection["name"], "role": role})
    return result
