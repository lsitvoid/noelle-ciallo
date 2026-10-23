"""终端输出：颜色、banner、流式写入、表格。Windows 自动启用 VT。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WIN = os.name == "nt"

if IS_WIN:
    try:
        import ctypes
        _k = ctypes.windll.kernel32
        _k.SetConsoleMode(_k.GetStdHandle(-11), 7)
        _VT = True
    except Exception:
        _VT = False
else:
    _VT = True

ENABLED = _VT and sys.stdout.isatty()
_NO_COLOR = os.environ.get("NO_COLOR") is not None or os.environ.get("NOELLE_NO_COLOR")
if _NO_COLOR:
    ENABLED = False


class C:
    reset = "\033[0m" if ENABLED else ""
    bold = "\033[1m" if ENABLED else ""
    dim = "\033[2m" if ENABLED else ""
    italic = "\033[3m" if ENABLED else ""
    red = "\033[31m" if ENABLED else ""
    green = "\033[32m" if ENABLED else ""
    yellow = "\033[33m" if ENABLED else ""
    blue = "\033[34m" if ENABLED else ""
    magenta = "\033[35m" if ENABLED else ""
    cyan = "\033[36m" if ENABLED else ""
    grey = "\033[90m" if ENABLED else ""


def out(s: str = "") -> None:
    print(s)


def err(s: str) -> None:
    print(f"{C.red}[错误]{C.reset} {s}", file=sys.stderr)


def banner() -> None:
    f = Path(__file__).parent.parent.parent / "assets" / "banner.txt"
    if f.is_file() and sys.stdout.isatty():
        print(C.cyan + C.bold)
        print(f.read_text(encoding="utf-8").rstrip("\n"))
        print(C.reset)


class stream_writer:
    """上下文管理器：流式写入，首次写入时自动换行缩进。"""

    def __init__(self, prefix: str = "", color: str = ""):
        self.prefix = prefix
        self.color = color or C.reset
        self.started = False

    def __enter__(self):
        return self

    def write(self, s: str) -> None:
        if not self.started:
            if self.prefix:
                sys.stdout.write(self.prefix)
            sys.stdout.write(self.color)
            self.started = True
        sys.stdout.write(s)
        sys.stdout.flush()

    def __exit__(self, *a):
        if self.started:
            sys.stdout.write(C.reset + "\n")
            sys.stdout.flush()


def prompt_input(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)


def table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("─" * w for w in widths)
    body = [
        "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r))
        for r in rows
    ]
    return "\n".join([line, sep] + body)
