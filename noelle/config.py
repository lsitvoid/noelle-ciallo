"""配置加载与合并。

配置文件是 JSONC（带注释的 JSON）。不引入第三方依赖，自己剥注释：
状态机扫描，跳过字符串内部，只删真正的 // 与 /* */ 注释。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 1,
    "active_provider": "",
    "active_model": "",
    "providers": [],
    "context": {
        "default_context_window": 128000,
        "compact_at_ratio": 0.85,
        "trim_at_ratio": 0.95,
        "on_overflow": "compact",
    },
    "tools": {
        "enabled": True,
        "max_rounds": 0,
        "default_timeout_secs": 60,
    },
    "mcp": {"enabled": False, "servers": []},
    "skills": {"enabled": True, "allow_command_execution": False},
    "display": {
        "language": "zh_CN",
        "expand_reasoning": False,
        "expand_tool_calls": True,
        "show_token_usage": True,
    },
    "notifications": {"enabled": True, "on_turn_complete": True},
    "prompt": {
        "prompts_dir": "",
        "active_persona": "",
        "persona_reminder": True,
    },
    "plugins": {},
    "system_prompt_file": "",
    "oobe_done": False,
    "terminal_session_mode": "normal",
    "voice": {"enabled": False, "wake_keywords": ["noelle"], "stt_language": "zh"},
}


def strip_jsonc(text: str) -> str:
    """移除 // 与 /* */ 注释，保留字符串内的内容。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load() -> dict[str, Any]:
    f = paths.config_file()
    if not f.is_file():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(strip_jsonc(f.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return merge(DEFAULT_CONFIG, user)


def merge(base: dict, over: dict) -> dict:
    """递归合并，over 覆盖 base。"""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def save(cfg: dict) -> None:
    paths.ensure_dirs()
    f = paths.config_file()
    if f.is_file():
        shutil.copy2(f, f.with_suffix(".jsonc.bak"))
    f.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        f.chmod(0o600)
    except OSError:
        pass


def get(key_path: str, default: Any = None) -> Any:
    """按点号路径取值，如 get('tools.enabled')。"""
    cur: Any = load()
    for part in key_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_value(key_path: str, value: Any) -> None:
    cfg = load()
    parts = key_path.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    save(cfg)
