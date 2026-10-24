"""Agent 主循环：把消息、工具、上下文压缩串起来。

一轮对话 = 
    构建 messages
    → 请求 LLM
    → 若返回 tool_calls → 执行工具 → 把结果回灌 → 再请求
    → 直到模型给出最终文本（或达到 max_rounds）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import config as cfgmod
from .llm import LLMClient, LLMError, Message, StreamEvent
from .storage import get_db
from .tools import registry as tool_registry


def _json_slice(text: str) -> str:
    """从模型回复中截出首个 JSON 对象"""
    s, e = text.find("{"), text.rfind("}")
    if 0 <= s < e:
        return text[s:e + 1]
    return text


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
        prof = self.profile_block()
        if prof:
            parts.append(prof)
        return "\n\n".join(parts)

    # ---------- 三类必读记忆 ----------
    #
    # 不把聊天记录堆进上下文，只读三类提炼后的关键信息：
    #   user    关于用户：性格、习惯、偏好、喜欢 AI 的方式
    #   self    关于 noelle 自己：性格、习惯、被喜欢/被讨厌的样子
    #   event   事件始末：不预注入，按当前输入检索 top-N

    def profile_block(self) -> str:
        db = get_db()
        persona = self.cfg.get("prompt", {}).get("active_persona", "")
        lines: list[str] = ["[必读记忆 · 每轮都生效]"]
        users = db.memories_by_kind("user", persona, limit=30)
        if users:
            lines.append("关于用户：" +
                         "；".join(r["content"] for r in users))
        selves = db.memories_by_kind("self", persona, limit=30)
        if selves:
            lines.append("关于你自己：" +
                         "；".join(r["content"] for r in selves))
        if len(lines) == 1:
            return ""
        text = "\n".join(lines)
        return text[:1500] if len(text) > 1500 else text

    def event_block(self, user_text: str) -> str:
        """按当前输入检索事件记忆，命中才注入"""
        db = get_db()
        persona = self.cfg.get("prompt", {}).get("active_persona", "")
        rows = db.search_events(user_text, persona, limit=3)
        if not rows:
            return ""
        return "[相关事件回忆]\n" + "\n".join(
            f"- {r['content']}" for r in rows
        )

    def distill(self, user_text: str, reply_text: str) -> None:
        """对话后提炼：只把关键信息写进记忆库（不存聊天原文）。

        三类：
        - user  用户画像更新（性格/习惯/偏好/喜欢 AI 的方式）
        - self  自己的画像更新（被喜欢/被讨厌的样子、该改的习惯）
        - event 有记忆点的事件始末（一句到三句话概括）

        已存在的内容不重复存：先按关键词检索去重。
        """
        if not self.cfg.get("memory", {}).get("auto_distill", True):
            return
        try:
            cli = self.client()
            comp = cli.complete([
                Message("system",
                        "你是记忆提炼器。从这轮对话提取值得长期记住的关键信息，"
                        "输出 JSON（不要多余文字）：\n"
                        '{"user": ["关于用户的新画像条目"...],'
                        ' "self": ["关于AI自己(尤其被喜欢/被讨厌的样子)的条目"...],'
                        ' "events": ["事件始末概括..."]}\n'
                        "规则：只存稳定、可复用的信息（性格/习惯/偏好/重要事件）；"
                        "临时闲聊、寒暄不要存；一般性知识问答（问历史/问原理/查资料）"
                        "全都不存；每条≤40字、必须是这句话独有的信息；"
                        "没有值得存的类别就留空数组。"),
                Message("user", f"用户说：{user_text[:500]}\nAI答：{reply_text[:500]}"),
            ], temperature=0.1)
            data = json.loads(_json_slice(comp.text))
        except Exception:  # noqa: BLE001 — 提炼失败不影响主对话
            return

        db = get_db()
        persona = self.cfg.get("prompt", {}).get("active_persona", "")
        for kind, items in (("user", data.get("user", [])),
                            ("self", data.get("self", [])),
                            ("event", data.get("events", []))):
            for it in items:
                it = str(it).strip()
                if not it or len(it) > 80:
                    continue
                # 去重：同 kind 下完全相同内容跳过
                dup = db.memories_by_kind(kind, persona, limit=200)
                if any(it == d["content"] for d in dup):
                    continue
                db.add_memory_typed(it, persona, kind, "auto")

    def recall_block(self, user_text: str) -> str:
        """事件始末：模型提到有印象的东西时自动翻库。
        事件检索 + 普通记忆检索各取若干。"""
        ev = self.event_block(user_text)
        if ev:
            return ev
        # 事件没命中，退回普通记忆关键词检索
        db = get_db()
        persona = self.cfg.get("prompt", {}).get("active_persona", "")
        rows = db.search_memories(user_text, persona, limit=2)
        if not rows:
            return ""
        return "[相关记忆]\n" + "\n".join(f"- {r['content']}" for r in rows)

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

        recall = self.recall_block(user_text)
        if recall:
            messages.append(Message("system", recall))

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
        # 只提炼关键信息入库，不存原文（上下文不会随历史膨胀）
        try:
            self.distill(user_text, turn.text)
        except Exception:  # noqa: BLE001
            pass
        if turn.prompt_tokens or turn.completion_tokens:
            pid, model, _ = self.resolve()
            db.record_usage(pid, model, turn.prompt_tokens, turn.completion_tokens)
        return turn
