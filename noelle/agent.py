"""Agent 主循环：把消息、工具、上下文压缩串起来。

一轮对话 = 
    构建 messages
    → 请求 LLM
    → 若返回 tool_calls → 执行工具 → 把结果回灌 → 再请求
    → 直到模型给出最终文本（或达到 max_rounds）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import config as cfgmod
from .llm import LLMClient, LLMError, Message, StreamEvent
from .storage import get_db
from .tools import registry as tool_registry


@dataclass
class Turn:
    """一轮对话的结果"""
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rounds: int = 0


Event = tuple[str, str]   # (kind, payload)  kind: text/reasoning/tool/tool_result/done/error


class Agent:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or cfgmod.load()
        from .plugins import load_all
        load_all(self.cfg)

    # ---------- 模型解析 ----------
    def resolve(self) -> tuple[str, str, str]:
        """返回 (provider_id, model, api_key)"""
        pid = self.cfg.get("active_provider") or ""
        model = self.cfg.get("active_model") or ""

        for entry in self.cfg.get("providers") or []:
            if entry.get("id") == pid:
                if not model:
                    models = entry.get("models") or []
                    model = models[0] if models else entry.get("model", "")
                key = entry.get("api_key", "")
                from .llm.client import resolve_api_key
                return pid, model, resolve_api_key(pid, key)

        if pid and model:
            from .llm.client import resolve_api_key
            return pid, model, resolve_api_key(pid)

        raise LLMError("尚未配置模型，请运行: noelle init 或 noelle config")

    def base_url(self, pid: str) -> str:
        for entry in self.cfg.get("providers") or []:
            if entry.get("id") == pid and entry.get("base_url"):
                return entry["base_url"]
        from .llm.providers import get_provider
        p = get_provider(pid)
        if p and p.base_url:
            return p.base_url
        raise LLMError(f"provider {pid} 缺少 base_url")

    def client(self) -> LLMClient:
        pid, model, key = self.resolve()
        return LLMClient(self.base_url(pid), key, model)

    # ---------- prompt 组装 ----------
    def persona_text(self) -> str:
        from . import paths
        name = self.cfg.get("prompt", {}).get("active_persona") or ""
        if not name:
            return ""
        f = paths.prompts_dir() / name
        if not f.is_file():
            return ""
        return f.read_text(encoding="utf-8", errors="replace").strip()

    def system_prompt(self) -> str:
        parts: list[str] = []
        spf = self.cfg.get("system_prompt_file") or ""
        if spf:
            from pathlib import Path
            f = Path(spf).expanduser()
            if f.is_file():
                parts.append(f.read_text(encoding="utf-8", errors="replace").strip())

        persona = self.persona_text()
        if persona:
            parts.append(persona)

        if self.cfg.get("tools", {}).get("enabled", True):
            names = ", ".join(tool_registry.names())
            parts.append(
                "你可以调用以下工具完成任务：" + names +
                "。需要时直接调用，不必询问；工具结果返回后继续完成任务。"
            )
        return "\n\n".join(parts)

    def build_messages(self, history: list[dict], user_text: str) -> list[Message]:
        msgs: list[Message] = []
        sp = self.system_prompt()
        if sp:
            msgs.append(Message("system", sp))
        for h in history:
            msgs.append(Message(
                h.get("role", "user"),
                h.get("content", ""),
                tool_calls=h.get("tool_calls") or None,
                tool_call_id=h.get("tool_call_id", ""),
            ))
        msgs.append(Message("user", user_text))
        return msgs

    # ---------- 主循环 ----------
    def run(
        self, user_text: str, history: list[dict] | None = None,
        stream: bool = True, on_event: Callable[[Event], None] | None = None,
    ) -> Turn:
        history = history or []
        messages = self.build_messages(history, user_text)
        cli = self.client()
        max_rounds = int(self.cfg.get("tools", {}).get("max_rounds") or 0) or 8
        tools_enabled = self.cfg.get("tools", {}).get("enabled", True)
        schemas = tool_registry.schemas() if tools_enabled else None
        temperature = float(self.cfg.get("temperature", 0.7))

        out = Turn()

        def emit(kind: str, payload: str) -> None:
            if on_event:
                on_event((kind, payload))

        for rnd in range(max_rounds):
            out.rounds = rnd + 1

            if stream:
                gen = cli.stream(messages, temperature=temperature, tools=schemas)
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                usage = (0, 0)
                try:
                    for ev in gen:
                        if ev.kind == "text":
                            text_parts.append(ev.text)
                            emit("text", ev.text)
                        elif ev.kind == "reasoning":
                            emit("reasoning", ev.text)
                        elif ev.kind == "tool_call":
                            tool_calls = ev.data.get("tool_calls", [])
                        elif ev.kind == "usage":
                            usage = (
                                ev.data.get("prompt_tokens", 0),
                                ev.data.get("completion_tokens", 0),
                            )
                except LLMError as e:
                    emit("error", str(e))
                    raise
                if not out.prompt_tokens:
                    out.prompt_tokens, out.completion_tokens = usage
                reply = "".join(text_parts)
            else:
                comp = cli.complete(messages, temperature=temperature, tools=schemas)
                reply = comp.text
                tool_calls = comp.tool_calls
                if not out.prompt_tokens:
                    out.prompt_tokens = comp.prompt_tokens
                    out.completion_tokens = comp.completion_tokens

            if not tool_calls:
                out.text = reply
                break

            # 有工具调用：先记 assistant 消息，再逐个执行
            messages.append(Message("assistant", reply or "", tool_calls=tool_calls))
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                emit("tool", f"{name}({raw_args})")
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
                    emit("tool_result", f"{name}: 参数不是合法 JSON")
                else:
                    res = tool_registry.call(name, args)
                    payload = res.to_llm()
                    emit("tool_result", f"{name}: {payload[:500]}")
                    messages.append(Message(
                        "tool", payload,
                        tool_call_id=tc.get("id", ""),
                    ))
                    out.tool_calls.append({"name": name, "args": args, "ok": res.ok})
                    continue
                messages.append(Message(
                    "tool", f"[工具参数错误] {raw_args}",
                    tool_call_id=tc.get("id", ""),
                ))
                out.tool_calls.append({"name": name, "args": {}, "ok": False})

            out.text = reply
        else:
            emit("done", "达到最大工具轮次")

        emit("done", "")
        return out

    # ---------- 会话封装 ----------
    def chat(
        self, session_id: str, user_text: str,
        stream: bool = True, on_event: Callable[[Event], None] | None = None,
    ) -> Turn:
        db = get_db()
        row = db.get_session(session_id)
        if row is None:
            # bridge / 前端首次接入：自动建档
            session_id = db.create_session(
                session_id, mode="bridge",
                persona=self.cfg.get("prompt", {}).get("active_persona", ""),
                provider=self.cfg.get("active_provider", ""),
                model=self.cfg.get("active_model", ""),
            )
        else:
            # 传入的可能是名字，统一换成真实 id
            session_id = row["id"]
        rows = db.messages(session_id)
        history = [
            {
                "role": r["role"], "content": r["content"],
                "tool_calls": json.loads(r["tool_calls"]) if r["tool_calls"] else None,
            }
            for r in rows
            if r["role"] in ("user", "assistant")
        ]

        turn = self.run(user_text, history, stream=stream, on_event=on_event)

        db.add_message(session_id, "user", user_text)
        db.add_message(
            session_id, "assistant", turn.text,
            reasoning=turn.reasoning,
            tool_calls=turn.tool_calls or None,
            tokens=turn.completion_tokens,
        )
        if turn.prompt_tokens or turn.completion_tokens:
            pid, model, _ = self.resolve()
            db.record_usage(pid, model, turn.prompt_tokens, turn.completion_tokens)
        return turn
