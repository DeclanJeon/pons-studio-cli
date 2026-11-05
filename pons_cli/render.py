"""Plain terminal output with external control sequences removed."""
from __future__ import annotations

import re
from typing import Final

OSC: Final = re.compile(r"\x1b\][\s\S]*?(?:\x07|\x1b\\)")
CSI: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def safe_text(value: str) -> str:
    """Strip terminal escape/control sequences, preserving Korean and newlines."""
    cleaned = CSI.sub("", OSC.sub("", value))
    return "".join(ch for ch in cleaned if ch in "\n\t" or (ord(ch) >= 32 and not 0x7f <= ord(ch) <= 0x9f))


def emit(value: str) -> None:
    print(safe_text(value), flush=True)
