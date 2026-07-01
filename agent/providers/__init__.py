"""
Providers 包 — LLM 提供商抽象层。
"""
from agent.providers.base import ProviderProtocol
from agent.providers.openai_provider import OpenAIProvider
from agent.providers.factory import create_provider, detect_provider_name, detect_base_url

__all__ = [
    "ProviderProtocol",
    "OpenAIProvider",
    "create_provider",
    "detect_provider_name",
    "detect_base_url",
]
