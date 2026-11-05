"""Explicitly prepare authorized production instructions, never provider runtimes."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

PIN: Final = "968b571161d44f52fbd91cf8f536096fc0e997c7"
UPSTREAM: Final = "https://github.com/DeclanJeon/contents-production-skills.git"


def prepare(source: Path, data: Path) -> Path:
    """Copy a pinned source atomically; leave the original and existing data untouched."""
    source = source.expanduser().resolve()
    data = data.expanduser().resolve()
    target = data / "skills/source"
    if target.exists():
        raise SystemExit("기존 스킬을 덮어쓰지 않습니다. 다른 --data를 선택하거나 기존 연결을 사용하세요.")
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise SystemExit("스킬 원본과 대상은 서로 포함할 수 없습니다.")
    marker = source / "installed-commit.txt"
    if marker.is_file():
        actual = marker.read_text().strip()
    else:
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True, timeout=10).strip()
        dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"], text=True, timeout=10)
        if dirty:
            raise SystemExit("원본 스킬 저장소에 변경이 있습니다. 원본을 수정하지 않고 중단합니다.")
    if actual != PIN:
        raise SystemExit("지원하는 고정 스킬 커밋과 다릅니다: " + PIN)
    manifest = json.loads((source / "manifest.json").read_text())
    names = manifest.get("skills")
    if not isinstance(names, list) or not names or len(names) > 100 or any(not isinstance(name, str) or re.fullmatch(r"[a-z0-9-]+", name) is None for name in names):
        raise SystemExit("스킬 manifest 형식이 올바르지 않습니다.")
    for name in names:
        folder = source / name
        if not (folder / "SKILL.md").is_file() or folder.is_symlink() or any(path.is_symlink() for path in folder.rglob("*")):
            raise SystemExit("실제 파일로 구성된 스킬 폴더가 필요합니다: " + name)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="instructions-", dir=target.parent) as temporary:
        staged = Path(temporary) / "source"
        staged.mkdir()
        for name in dict.fromkeys(names):
            shutil.copytree(source / name, staged / name)
        for name in ("manifest.json", "LICENSE", "README.md"):
            if (source / name).is_file():
                shutil.copyfile(source / name, staged / name)
        (staged / "installed-commit.txt").write_text(PIN + "\n")
        staged.rename(target)
    return target


def main() -> int:
    """Network access requires an explicit --download; local sources are never changed."""
    parser = argparse.ArgumentParser(prog="pons-skills", description="고정 제작 스킬 준비 (모델/공급자 설치 없음)")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--source", type=Path, help="권한 있는 기존 스킬 소스/설치 폴더")
    choice.add_argument("--download", action="store_true", help="원본 저장소를 명시적으로 내려받기 (접근 권한 필요)")
    default = os.environ.get("STUDIO_DATA") or str(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "pons-studio")
    parser.add_argument("--data", type=Path, default=Path(default))
    args = parser.parse_args()
    if args.source:
        target = prepare(args.source, args.data)
    else:
        with tempfile.TemporaryDirectory(prefix="pons-skills-") as temporary:
            source = Path(temporary) / "source"
            subprocess.run(["git", "clone", "--filter=blob:none", UPSTREAM, str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "checkout", "--detach", PIN], check=True)
            target = prepare(source, args.data)
    print("제작 스킬 준비 완료: " + str(target))
    print("외부 codex-imagen/ai-delegation-loop·MCP·공급자 계정은 별도 연결이며 자동 설치하지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
