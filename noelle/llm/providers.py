"""Provider 注册表。

所有 provider 走 OpenAI 兼容协议（/chat/completions）。
要加新的，往 PROVIDERS 里加一条即可，不用改客户端代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Provider:
    id: str
    name: str
    base_url: str
    models: list[str] = field(default_factory=list)
    api_key_env: str = ""
    supports_stream: bool = True
    supports_tools: bool = True
    supports_vision: bool = False
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "zhipu": Provider(
        id="zhipu", name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=["glm-4-flash", "glm-4-plus", "glm-4-air"],
        api_key_env="ZHIPU_API_KEY",
        supports_vision=True,
        note="国内直连",
    ),
    "deepseek": Provider(
        id="deepseek", name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        models=["deepseek-chat", "deepseek-reasoner"],
        api_key_env="DEEPSEEK_API_KEY",
        note="国内直连",
    ),
    "openai": Provider(
        id="openai", name="OpenAI",
        base_url="https://api.openai.com/v1",
        models=["gpt-4o", "gpt-4o-mini"],
        api_key_env="OPENAI_API_KEY",
        supports_vision=True,
    ),
    "openrouter": Provider(
        id="openrouter", name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        models=["anthropic/claude-3.5-sonnet", "google/gemini-pro-1.5"],
        api_key_env="OPENROUTER_API_KEY",
        supports_vision=True,
        note="聚合多家",
    ),
    "ollama": Provider(
        id="ollama", name="Ollama (本地)",
        base_url="http://localhost:11434/v1",
        models=["qwen2.5:7b", "llama3.2:3b"],
        api_key_env="",
        supports_vision=True,
        note="本地推理，无需 key",
    ),
    "custom": Provider(
        id="custom", name="自定义",
        base_url="",
        models=[],
        note="任何 OpenAI 兼容端点",
    ),
}


def get_provider(pid: str) -> Provider | None:
    return PROVIDERS.get(pid)


def find_provider_for_model(model: str) -> tuple[Provider, str] | None:
    """裸模型名 → (provider, model)"""
    for p in PROVIDERS.values():
        if model in p.models:
            return p, model
    return None
