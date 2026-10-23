"""OpenAI 兼容客户端：流式与非流式、工具调用、重试。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Generator, Iterator


class LLMError(RuntimeError):
    pass


@dataclass
class Message:
    role: str
    content: str = ""
    name: str = ""
    tool_calls: list[dict] | None = None
    tool_call_id: str = ""

    def to_api(self) -> dict:
        d: dict[str, Any] = {"role": self.role}
        if self.content:
            d["content"] = self.content
        if self.name:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass
class StreamEvent:
    kind: str               # text | reasoning | tool_call | usage | done | error
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Completion:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""


class LLMClient:
    def __init__(
        self, base_url: str, api_key: str = "", model: str = "",
        timeout: int = 180, retries: int = 1,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = retries

    # ---------- 底层请求 ----------
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _payload(
        self, messages: list[Message], stream: bool,
        temperature: float, max_tokens: int | None,
        tools: list[dict] | None,
    ) -> dict:
        p: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_api() for m in messages],
            "stream": stream,
            "temperature": temperature,
        }
        if max_tokens:
            p["max_tokens"] = max_tokens
        if tools:
            p["tools"] = tools
            p["tool_choice"] = "auto"
        return p

    def _open(self, payload: dict):
        url = f"{self.base_url}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _retry(self, fn):
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return fn()
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:500]
                last = LLMError(f"HTTP {e.code} — {body}")
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last from None
            except urllib.error.URLError as e:
                last = LLMError(f"网络不可达: {e.reason}")
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last from None
            except TimeoutError:
                last = LLMError("请求超时")
                if attempt < self.retries:
                    continue
                raise last from None
        raise last or LLMError("未知错误")

    # ---------- 非流式 ----------
    def complete(
        self, messages: list[Message], temperature: float = 0.7,
        max_tokens: int | None = None, tools: list[dict] | None = None,
    ) -> Completion:
        payload = self._payload(messages, False, temperature, max_tokens, tools)

        def go() -> Completion:
            with self._open(payload) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
            choices = obj.get("choices") or []
            if not choices:
                raise LLMError(f"响应无 choices: {str(obj)[:300]}")
            ch = choices[0]
            msg = ch.get("message") or {}
            usage = obj.get("usage") or {}
            return Completion(
                text=msg.get("content") or "",
                reasoning=msg.get("reasoning_content") or "",
                tool_calls=msg.get("tool_calls") or [],
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                finish_reason=ch.get("finish_reason") or "",
            )

        return self._retry(go)

    # ---------- 流式 ----------
    def stream(
        self, messages: list[Message], temperature: float = 0.7,
        max_tokens: int | None = None, tools: list[dict] | None = None,
    ) -> Iterator[StreamEvent]:
        payload = self._payload(messages, True, temperature, max_tokens, tools)
        acc_tools: dict[int, dict] = {}
        usage: dict = {}

        def go():
            return self._open(payload)

        resp = self._retry(go)
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if obj.get("usage"):
                    usage = obj["usage"]

                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}

                    if delta.get("reasoning_content"):
                        yield StreamEvent("reasoning", delta["reasoning_content"])

                    if delta.get("content"):
                        yield StreamEvent("text", delta["content"])

                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0))
                        slot = acc_tools.setdefault(
                            idx, {"id": "", "type": "function",
                                  "function": {"name": "", "arguments": ""}}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]

            if acc_tools:
                calls = [acc_tools[k] for k in sorted(acc_tools)]
                yield StreamEvent("tool_call", data={"tool_calls": calls})

            if usage:
                yield StreamEvent(
                    "usage",
                    data={
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                    },
                )
            yield StreamEvent("done")
        finally:
            resp.close()

    # ---------- 汇聚 ----------
    def collect(self, events: Iterator[StreamEvent]) -> Completion:
        """把流事件汇聚成完整结果"""
        c = Completion()
        for ev in events:
            if ev.kind == "text":
                c.text += ev.text
            elif ev.kind == "reasoning":
                c.reasoning += ev.text
            elif ev.kind == "tool_call":
                c.tool_calls = ev.data.get("tool_calls", [])
            elif ev.kind == "usage":
                c.prompt_tokens = ev.data.get("prompt_tokens", 0)
                c.completion_tokens = ev.data.get("completion_tokens", 0)
        return c


def resolve_api_key(provider_id: str, cfg_key: str = "") -> str:
    """配置里的 key 优先，其次环境变量。"""
    if cfg_key:
        return cfg_key
    from .providers import get_provider
    p = get_provider(provider_id)
    if p and p.api_key_env:
        return os.environ.get(p.api_key_env, "")
    return ""


def list_models(cfg: dict) -> list[tuple[str, str, str]]:
    """返回 [(provider_id, model, 来源)]"""
    out: list[tuple[str, str, str]] = []
    for entry in cfg.get("providers") or []:
        pid = entry.get("id", "")
        name = entry.get("name") or pid
        for m in entry.get("models") or []:
            out.append((pid, m, name))
        if not entry.get("models") and entry.get("model"):
            out.append((pid, entry["model"], name))
    if not out:
        from .providers import PROVIDERS
        for p in PROVIDERS.values():
            for m in p.models:
                out.append((p.id, m, p.name))
    return out
