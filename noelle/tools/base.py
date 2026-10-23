"""工具抽象与注册表。

每个工具描述自己的 JSON Schema，LLM 通过 function calling 决定调用哪个。

扩展插件只需：

    from noelle.tools import registry, Tool

    @registry.register
    class MyTool(Tool):
        name = "my_tool"
        description = "做某事"
        parameters = {"type": "object", "properties": {...}}
        def run(self, **kwargs): ...
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    error: str = ""
    elapsed: float = 0.0

    def to_llm(self) -> str:
        if self.ok:
            return self.content
        return f"[工具错误] {self.error}"


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)  # type: ignore[assignment]
    dangerous: bool = False
    timeout: int = 60

    def __init__(self) -> None:
        if not self.parameters:
            self.parameters = {"type": "object", "properties": {}}

    def run(self, **kwargs: Any) -> str:  # pragma: no cover - 子类实现
        raise NotImplementedError

    async def arun(self, **kwargs: Any) -> str:
        """默认把同步 run 丢线程池，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.run, **kwargs)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, args: dict | None = None) -> ToolResult:
        args = args or {}
        t0 = time.time()
        try:
            out = self.run(**args)
            if inspect.isawaitable(out):
                out = asyncio.get_event_loop().run_until_complete(out)
            return ToolResult(True, str(out), "", time.time() - t0)
        except TypeError as e:
            return ToolResult(False, "", f"参数错误: {e}", time.time() - t0)
        except Exception as e:  # noqa: BLE001 - 工具隔离，任何异常都不能炸掉主循环
            return ToolResult(False, "", f"{type(e).__name__}: {e}", time.time() - t0)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool_cls: type[Tool] | Tool) -> type[Tool] | Tool:
        inst = tool_cls() if isinstance(tool_cls, type) else tool_cls
        if not inst.name:
            raise ValueError(f"{inst.__class__.__name__} 缺少 name")
        self._tools[inst.name] = inst
        return tool_cls

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, enabled: list[str] | None = None) -> list[dict]:
        tools = self.all()
        if enabled is not None:
            tools = [t for t in tools if t.name in enabled]
        return [t.schema() for t in tools]

    def call(self, name: str, args: dict | None = None) -> ToolResult:
        t = self.get(name)
        if not t:
            return ToolResult(False, "", f"未知工具: {name}")
        return t.execute(args)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)


registry = ToolRegistry()
