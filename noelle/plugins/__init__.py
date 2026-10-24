"""插件系统：目录化、可启停。

三个来源，按优先级：

1. **内置插件**  `noelle/plugins/builtin_plugins/`（随项目分发）
2. **用户插件**  `<NOELLE_HOME>/extensions/*.py`
3. **脚本工具**  `<NOELLE_HOME>/extensions/scripts/`（可执行文件）

启停：配置文件 `"plugins": {"<插件名>": {"enabled": false}}`，
或命令 `noelle plugins enable/disable <名>`。
默认：内置插件全启用，用户插件全启用，脚本工具全启用。

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

BUILTIN_DIR = Path(__file__).parent / "builtin_plugins"


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


def _enabled(name: str, cfg: dict) -> bool:
    return bool(cfg.get("plugins", {}).get(name, {}).get("enabled", True))


def load_builtin_plugins(cfg: dict | None = None) -> list[str]:
    """加载内置插件目录，受配置启停"""
    loaded: list[str] = []
    cfg = cfg or {}
    if not BUILTIN_DIR.is_dir():
        return loaded
    for f in sorted(BUILTIN_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        name = f"builtin:{f.stem}"
        if not _enabled(name, cfg):
            continue
        mod_name = f"noelle_bplug_{f.stem}"
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
            loaded.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"[内置插件加载失败] {f.name}: {e}", file=sys.stderr)
    return loaded


def load_python_plugins(cfg: dict | None = None) -> list[str]:
    """加载 extensions/*.py，受配置启停"""
    loaded: list[str] = []
    cfg = cfg or {}
    d = paths.extensions_dir()
    if not d.is_dir():
        return loaded
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        name = f"user:{f.stem}"
        if not _enabled(name, cfg):
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
            loaded.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"[插件加载失败] {f.name}: {e}", file=sys.stderr)
    return loaded


def load_script_plugins(cfg: dict | None = None) -> list[str]:
    """加载 extensions/scripts/ 下的可执行文件"""
    cfg = cfg or {}
    loaded: list[str] = []
    d = paths.extensions_dir() / "scripts"
    if not d.is_dir():
        return loaded
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        if not (f.stat().st_mode & 0o111):
            continue
        name = f"script:{f.stem}"
        if not _enabled(name, cfg):
            continue
        try:
            tool = ScriptTool(f)
            registry.register(tool)
            loaded.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"[脚本工具加载失败] {f.name}: {e}", file=sys.stderr)
    return loaded


def load_all(cfg: dict | None = None) -> dict[str, list[str]]:
    paths.ensure_dirs()
    (paths.extensions_dir() / "scripts").mkdir(exist_ok=True)
    return {
        "builtin": load_builtin_plugins(cfg),
        "python": load_python_plugins(cfg),
        "scripts": load_script_plugins(cfg),
    }


def discover() -> list[dict]:
    """枚举所有插件及其启用状态（不加载）"""
    from .. import config as cfgmod
    cfg = cfgmod.load()
    out: list[dict] = []
    for f in sorted(BUILTIN_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        name = f"builtin:{f.stem}"
        out.append({"name": name, "enabled": _enabled(name, cfg),
                    "source": str(BUILTIN_DIR / f.name)})
    d = paths.extensions_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            name = f"user:{f.stem}"
            out.append({"name": name, "enabled": _enabled(name, cfg),
                        "source": str(f)})
        sd = d / "scripts"
        if sd.is_dir():
            for f in sorted(sd.iterdir()):
                if f.is_file() and (f.stat().st_mode & 0o111):
                    name = f"script:{f.stem}"
                    out.append({"name": name, "enabled": _enabled(name, cfg),
                                "source": str(f)})
    return out


def set_enabled(name: str, enabled: bool) -> bool:
    """按名启用/停用。名可带前缀（builtin:render）也可不带（render）。"""
    from .. import config as cfgmod
    cfg = cfgmod.load()
    cur = cfg.setdefault("plugins", {})
    key = None
    for k in cur:
        if k == name or k.split(":", 1)[-1] == name:
            key = k
            break
    if key is None:
        # 按来源补前缀
        if name.startswith(("builtin:", "user:", "script:")):
            key = name
        else:
            if (BUILTIN_DIR / f"{name}.py").is_file():
                key = f"builtin:{name}"
            elif (paths.extensions_dir() / f"{name}.py").is_file():
                key = f"user:{name}"
            elif (paths.extensions_dir() / "scripts" / name).exists():
                key = f"script:{name}"
            else:
                return False
    cur.setdefault(key, {})["enabled"] = enabled
    cfgmod.save(cfg)
    return True


__all__ = [
    "load_all", "load_builtin_plugins", "load_python_plugins",
    "load_script_plugins", "discover", "set_enabled", "ScriptTool",
]
