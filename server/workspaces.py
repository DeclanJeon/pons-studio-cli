"""Shared workspace confinement for desktop, HTTP and terminal clients."""
from __future__ import annotations

from pathlib import Path

from .store import Problem


def checked_directory(value: str) -> Path:
    """Reject symlink components before canonicalizing an operator-selected path."""
    original = Path(value).expanduser().absolute()
    for component in (*reversed(original.parents), original):
        if component.is_symlink():
            raise Problem("심볼릭 링크가 아닌 실제 작업 폴더를 선택하세요.", 403)
    path = original.resolve()
    if path.exists() and not path.is_dir():
        raise Problem("작업 폴더 경로에 파일이 있습니다.", 409)
    return path


def workspace_directory(value: str, data: Path) -> Path:
    """Keep the private control root and production output tree disjoint."""
    path = checked_directory(value)
    private = data.resolve()
    if private.is_relative_to(path) or path.is_relative_to(private):
        raise Problem("작업 폴더와 개인 데이터 폴더는 서로 포함할 수 없습니다.", 403)
    checked_directory(str(path / "pons-projects"))
    return path
