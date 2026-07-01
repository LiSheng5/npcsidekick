"""
Provider Factory — 按模型名自动推断 Provider。

规则 (按优先级):
  1. 如果显式指定 provider_name → 使用对应 Provider
  2. 如果 model_name 含 "deepseek" → OpenAIProvider(api.deepseek.com)
  3. 如果 model_name 含 "gpt" | "openai" | "o1" | "o3" → OpenAIProvider(api.openai.com)
  4. 否则使用 base_url (已配置的默认值) → OpenAIProvider

所有 Provider 都使用 OpenAI 兼容 API 格式 — DeepSeek 的 API 与 OpenAI 兼容。
"""

from __future__ import annotations

from typing import Optional

from agent.providers.base import ProviderProtocol
from agent.providers.openai_provider import OpenAIProvider


# 已知 provider 的默认 base_url 映射
_KNOWN_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
}


def detect_provider_name(model_name: str) -> str:
    """按模型名推断 provider 名称。"""
    lower = model_name.lower()
    if "deepseek" in lower:
        return "deepseek"
    if any(kw in lower for kw in ("gpt", "openai", "o1", "o3", "o4")):
        return "openai"
    return "unknown"  # 未知模型 — 由调用方决定如何处理


def detect_base_url(model_name: str, configured_url: str = "") -> str:
    """
    按模型名推断 base_url。

    优先级:
      1. 已知 provider (deepseek/gpt 等) → 使用其默认 URL (忽略配置)
      2. 未知 provider + 有配置 URL → 使用配置
      3. 未知 provider + 无配置 → 返回空字符串

    设计理由: 模型名本身就包含了 provider 信息 — "deepseek-chat" 只能
    在 api.deepseek.com 访问，"gpt-4" 只能在 api.openai.com。用户配置的
    base_url 仅适用于无法自动推断的自建/代理服务。
    """
    provider = detect_provider_name(model_name)
    if provider in _KNOWN_BASE_URLS:
        return _KNOWN_BASE_URLS[provider]
    return configured_url  # unknown → use configured (may be empty)


def create_provider(
    api_key: str,
    model_name: str,
    base_url: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    provider_name: str = "auto",
) -> ProviderProtocol:
    """
    创建 Provider 实例。

    Args:
      api_key: API 密钥
      model_name: 模型名 (如 "deepseek-chat", "gpt-4")
      base_url: API 端点 (如未提供则按模型名推断)
      temperature: 温度参数
      max_tokens: 最大 token 数
      provider_name: "auto" | "openai" | "deepseek" — 显式指定 provider

    Returns:
      ProviderProtocol 实例

    示例:
      # 自动检测
      p = create_provider("sk-...", "deepseek-chat")
      # → OpenAIProvider(base_url="https://api.deepseek.com")

      # 显式指定
      p = create_provider("sk-...", "gpt-4", provider_name="openai")

      # 自建服务
      p = create_provider("sk-...", "custom-model", base_url="http://localhost:8080/v1")
    """
    if not base_url:
        base_url = detect_base_url(model_name)

    # 所有已知 provider 都使用 OpenAI 兼容 API
    return OpenAIProvider(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
    )
