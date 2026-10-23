"""插件系统。

两种扩展方式：

1. **Python 插件**（进程内）
   在 <NOELLE_HOME>/extensions/ 下放一个 .py 文件，定义 setup(registry) 函数：

       from noelle.tools import registry, Tool

       class MyTool(Tool):
           name = "my_tool"
           description = "做某事"
           parameters = {"type":"object","properties":{}}
           def run(self): return "hello"

       def setup(reg):
           reg.register(MyTool)

2. **脚本工具**（子进程）
   在 <NOELLE_HOME>/extensions/scripts/ 下放可执行文件，
   文件名即工具名，读 stdin 的 JSON 参数，输出结果到 stdout。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .. import paths
from ..tools import Tool, registry


class ScriptTool(Tool):
    """把可执行脚本包装成工具。参数整体以 JSON 从 stdin 传入。"""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem.replace("-", "_")
        self.description = f"脚本工具: {path.name}"
        self.parameters = {
            "type": "object",
            "properties": {"args": {"type": "string", "description": "传给脚本的参数"}},
        }

    def run(self, args: str = "") -> str:
        r = subprocess.run(
            [str(self.path)],
            input=json.dumps({"args": args}, ensure_ascii=False),
            capture_output=True, text=True, timeout=self.timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(f"脚本退出码 {r.returncode}: {r.stderr.strip()[:500]}")
        return r.stdout.strip()


def load_python_plugins() -> list[str]:
    """加载 extensions/*.py，返回已加载的模块名"""
    loaded: list[str] = []
    d = paths.extensions_dir()
    if not d.is_dir():
        return loaded
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        mod_name = f"noelle_ext_{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, f)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            setup = getattr(mod, "setup", None)
            if callable(setup):
                setup(registry)
            loaded.append(f.stem)
        except Exception as e:  # noqa: BLE001
            print(f"[插件加载失败] {f.name}: {e}", file=sys.stderr)
    return loaded


def load_script_plugins() -> list[str]:
    """加载 extensions/scripts/ 下的可执行文件"""
    loaded: list[str] = []
    d = paths.extensions_dir() / "scripts"
    if not d.is_dir():
        return loaded
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        if not (f.stat().st_mode & 0o111):
            continue
        try:
            tool = ScriptTool(f)
            registry.register(tool)
            loaded.append(tool.name)
        except Exception as e:  # noqa: BLE001
            print(f"[脚本工具加载失败] {f.name}: {e}", file=sys.stderr)
    return loaded


def load_all() -> dict[str, list[str]]:
    paths.ensure_dirs()
    (paths.extensions_dir() / "scripts").mkdir(exist_ok=True)
    py = load_python_plugins()
    sc = load_script_plugins()
    return {"python": py, "scripts": sc}


__all__ = ["load_all", "load_python_plugins", "load_script_plugins", "ScriptTool"]
