"""路径解析。

所有路径集中在这里，方便测试时用 NOELLE_HOME 整体重定向。

布局（NOELLE_HOME 默认 ~/.noelle）:

    <home>/config/config.jsonc     主配置
    <home>/data/prompts/           人格提示词
    <home>/data/kb/                知识库
    <home>/state/                  运行状态
    <home>/cache/                  缓存与日志
    <home>/extensions/             用户插件
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    env = os.environ.get("NOELLE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".noelle").resolve()


def config_dir() -> Path:
    return home() / "config"


def config_file() -> Path:
    return config_dir() / "config.jsonc"


def data_dir() -> Path:
    return home() / "data"


def prompts_dir() -> Path:
    return data_dir() / "prompts"


def kb_dir() -> Path:
    return data_dir() / "kb"


def state_dir() -> Path:
    return home() / "state"


def cache_dir() -> Path:
    return home() / "cache"


def logs_dir() -> Path:
    return cache_dir() / "logs"


def extensions_dir() -> Path:
    return home() / "extensions"


def runtime_dir() -> Path:
    """daemon socket / pid 所在目录"""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "noelle"
    return state_dir() / "runtime"


def db_file() -> Path:
    return state_dir() / "conversation.db"


def ensure_dirs() -> None:
    for d in (
        config_dir(), data_dir(), prompts_dir(), kb_dir(),
        state_dir(), cache_dir(), logs_dir(), extensions_dir(),
        runtime_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)


def all_paths() -> dict[str, str]:
    return {
        "home": str(home()),
        "config": str(config_file()),
        "data": str(data_dir()),
        "prompts": str(prompts_dir()),
        "kb": str(kb_dir()),
        "state": str(state_dir()),
        "cache": str(cache_dir()),
        "logs": str(logs_dir()),
        "runtime": str(runtime_dir()),
        "db": str(db_file()),
        "extensions": str(extensions_dir()),
    }
