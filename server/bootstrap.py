"""Import an explicitly prepared local skill package, never sibling-project files."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Final

from .store import Store

PIN: Final = "968b571161d44f52fbd91cf8f536096fc0e997c7"


def seed_skills(store: Store, app_root: Path) -> bool:
    """Copy a pinned local source when configured; never download or inspect siblings."""
    target = store.root / "skills/source"
    if target.exists():
        return False
    selected = os.environ.get("PONS_SKILLS_SOURCE")
    source = Path(selected).expanduser().resolve() if selected else app_root / "skills"
    marker = source / "installed-commit.txt"
    if not marker.is_file() or marker.read_text().strip() != PIN or not (source / "creative-production/SKILL.md").is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="instructions-", dir=target.parent) as staging:
        copied = Path(staging) / "source"
        shutil.copytree(source, copied)
        copied.rename(target)
    return True
