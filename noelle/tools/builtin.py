"""内置工具集。

  文件读写、目录遍历、搜索、命令执行、网页抓取、记忆、知识库、时间。
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .base import Tool, registry

MAX_OUTPUT = 30_000


def _truncate(s: str, n: int = MAX_OUTPUT) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n…（已截断，共 {len(s)} 字符）"


@registry.register
class ReadFile(Tool):
    name = "read_file"
    description = "读取文本文件内容。支持指定行范围。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，支持 ~ 展开"},
            "start": {"type": "integer", "description": "起始行，1 开始"},
            "limit": {"type": "integer", "description": "读取行数"},
        },
        "required": ["path"],
    }

    def run(self, path: str, start: int = 1, limit: int = 0) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在: {p}")
        if p.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("文件超过 20MB，请用读取行范围或自行处理")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(0, start - 1)
        e = s + limit if limit > 0 else len(lines)
        chunk = lines[s:e]
        numbered = [f"{s + i + 1}\t{ln}" for i, ln in enumerate(chunk)]
        return _truncate("\n".join(numbered))


@registry.register
class WriteFile(Tool):
    name = "write_file"
    description = "写入文本文件。mode=overwrite 覆盖，mode=append 追加。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append"]},
        },
        "required": ["path", "content"],
    }

    def run(self, path: str, content: str, mode: str = "overwrite") -> str:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            p.write_text(content, encoding="utf-8")
        return f"已写入 {p}（{len(content)} 字符）"


@registry.register
class EditFile(Tool):
    name = "edit_file"
    description = "在文件中把 old 替换为 new。old 必须唯一匹配。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
            "count": {"type": "integer", "description": "替换次数，0 表示全部"},
        },
        "required": ["path", "old", "new"],
    }

    def run(self, path: str, old: str, new: str, count: int = 1) -> str:
        p = Path(path).expanduser()
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n == 0:
            raise ValueError("未找到要替换的内容")
        if count == 1 and n > 1:
            raise ValueError(f"old 匹配到 {n} 处，请提供更长上下文或指定 count")
        p.write_text(text.replace(old, new, count or -1), encoding="utf-8")
        return f"已替换 {n if count == 0 else min(count, n)} 处"


@registry.register
class ListDir(Tool):
    name = "list_dir"
    description = "列出目录内容。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认当前目录"},
            "all": {"type": "boolean", "description": "是否包含隐藏文件"},
        },
    }

    def run(self, path: str = ".", all: bool = False) -> str:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise NotADirectoryError(f"不是目录: {p}")
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        out = []
        for it in items:
            if not all and it.name.startswith("."):
                continue
            if it.is_dir():
                out.append(f"{it.name}/")
            else:
                out.append(f"{it.name}\t{it.stat().st_size}")
        return "\n".join(out) or "(空目录)"


@registry.register
class FindFiles(Tool):
    name = "find_files"
    description = "按名称模式递归查找文件。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，如 *.py"},
            "path": {"type": "string", "description": "起始目录"},
            "limit": {"type": "integer"},
        },
        "required": ["pattern"],
    }

    def run(self, pattern: str, path: str = ".", limit: int = 200) -> str:
        root = Path(path).expanduser()
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fnmatch.fnmatch(fn, pattern):
                    hits.append(str(Path(dirpath) / fn))
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n（已达上限 {limit}）"
        return "\n".join(hits) or "未找到匹配文件"


@registry.register
class Grep(Tool):
    name = "grep"
    description = "在文件中搜索文本（正则）。优先用 ripgrep，否则纯 Python 回退。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string"},
            "glob": {"type": "string", "description": "文件过滤，如 *.py"},
            "limit": {"type": "integer"},
        },
        "required": ["pattern"],
    }

    def run(self, pattern: str, path: str = ".", glob: str = "", limit: int = 100) -> str:
        path = str(Path(path).expanduser())
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-m", "5"]
            if glob:
                cmd += ["-g", glob]
            cmd += [pattern, path]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                lines = r.stdout.splitlines()[:limit]
                return _truncate("\n".join(lines)) or "无匹配"
            except subprocess.TimeoutExpired:
                pass
        import re
        rx = re.compile(pattern)
        out: list[str] = []
        root = Path(path).expanduser()
        files = [root] if root.is_file() else root.rglob(glob or "*")
        for f in files:
            if not f.is_file() or f.stat().st_size > 5 * 1024 * 1024:
                continue
            try:
                for i, ln in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(ln):
                        out.append(f"{f}:{i}:{ln}")
                        if len(out) >= limit:
                            return _truncate("\n".join(out))
            except OSError:
                continue
        return _truncate("\n".join(out)) or "无匹配"


@registry.register
class RunCommand(Tool):
    name = "run_command"
    description = "执行 shell 命令并返回输出。危险操作需谨慎。"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 60"},
            "cwd": {"type": "string"},
        },
        "required": ["command"],
    }

    def run(self, command: str, timeout: int = 60, cwd: str = "") -> str:
        if cwd:
            cwd = str(Path(cwd).expanduser())
        try:
            r = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cwd or None,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"命令超时（{timeout}s）") from None
        parts = []
        if r.stdout:
            parts.append(r.stdout.rstrip())
        if r.stderr:
            parts.append(f"[stderr]\n{r.stderr.rstrip()}")
        parts.append(f"[退出码 {r.returncode}]")
        return _truncate("\n".join(parts))


@registry.register
class WebFetch(Tool):
    name = "web_fetch"
    description = "抓取网页，返回纯文本（去标签）。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        "required": ["url"],
    }

    def run(self, url: str, max_chars: int = 20000) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "noelle/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        import re
        raw = re.sub(r"<script.*?</script>", "", raw, flags=re.S | re.I)
        raw = re.sub(r"<style.*?</style>", "", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        return _truncate(text, max_chars)


@registry.register
class Now(Tool):
    name = "now"
    description = "获取当前日期时间。"
    parameters = {"type": "object", "properties": {}}

    def run(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S %Z")


@registry.register
class RememberMemory(Tool):
    name = "remember_memory"
    description = "把一条事实存入长期记忆。"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容"},
            "persona": {"type": "string"},
        },
        "required": ["content"],
    }

    def run(self, content: str, persona: str = "") -> str:
        from ..storage import get_db
        mem_id = get_db().add_memory(content, persona=persona, source="tool")
        return f"已记住（id={mem_id}）"


@registry.register
class RecallMemory(Tool):
    name = "recall_memory"
    description = "检索长期记忆。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "persona": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    }

    def run(self, query: str, persona: str = "", limit: int = 10) -> str:
        from ..storage import get_db
        rows = get_db().search_memories(query, persona=persona, limit=limit)
        if not rows:
            return "无相关记忆"
        return "\n".join(f"[{r['id']}] {r['content']}" for r in rows)


@registry.register
class SearchKB(Tool):
    name = "search_kb"
    description = "搜索本地知识库。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    }

    def run(self, query: str, limit: int = 5) -> str:
        from ..storage import get_db
        rows = get_db().search_kb(query, limit=limit)
        if not rows:
            return "知识库无相关条目"
        out = []
        for r in rows:
            snippet = r["content"][:400]
            out.append(f"## {r['title']}\n{snippet}")
        return "\n\n".join(out)


@registry.register
class SystemInfo(Tool):
    name = "system_info"
    description = "获取系统基本信息（发行版、内核、内存、磁盘）。"
    parameters = {"type": "object", "properties": {}}

    def run(self) -> str:
        info = {
            "platform": sys.platform,
            "python": sys.version.split()[0],
        }
        try:
            rel = Path("/etc/os-release")
            if rel.is_file():
                for ln in rel.read_text().splitlines():
                    if ln.startswith("PRETTY_NAME="):
                        info["distro"] = ln.split("=", 1)[1].strip('"')
        except OSError:
            pass
        try:
            info["kernel"] = os.uname().release
        except AttributeError:
            info["kernel"] = "windows"
        try:
            import shutil as sh
            du = sh.disk_usage(str(Path.home()))
            info["disk_free_gb"] = round(du.free / 1e9, 1)
            info["disk_total_gb"] = round(du.total / 1e9, 1)
        except OSError:
            pass
        return json.dumps(info, ensure_ascii=False, indent=2)
