"""Resolve production workspaces without mixing them with credentials."""
from __future__ import annotations

import os
from pathlib import Path

from server.workspaces import workspace_directory as workspace_directory


def data_directory(explicit: str | None, app_root: Path) -> Path:
    """Select private data without relocating any existing installation."""
    selected = explicit or os.environ.get("STUDIO_DATA")
    if selected:
        return Path(selected).expanduser().resolve()
    legacy = app_root / ".studio"
    if legacy.is_dir():
        return legacy.resolve()
    return (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "pons-studio").resolve()
