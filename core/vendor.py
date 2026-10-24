"""厂商探测的单一来源 —— 模型名关键词与端点域名两路信号只此一份。

v4 不绑定厂商，这里只做两件事：
  1. 没配端点时按模型名认厂商，用内置端点兜底（factory.create_provider）
  2. 思考参数的「方言」判定（DeepSeek 系 / OpenAI 官方 / OpenRouter / GLM 各不同）——
     openai_provider 只消费这里的标签，不再自己按域名重猜一遍

这两路信号此前分家：factory 按模型名关键词猜（选端点），openai_provider 按
base_url 域名再猜一遍（选方言），边界对不齐（自建网关跑 glm 时 provider 认得、
factory 却推不出端点）。现在统一收在本模块。
"""
from __future__ import annotations

# 已知 provider 的默认 base_url（仅在用户没配 NPC_BASE_URL 时兜底）
KNOWN_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

# 各 provider 的模型名关键词（顺序 = 匹配优先级）
PROVIDER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "deepseek": ("deepseek",),
    "zhipu": ("glm", "zhipu", "chatglm"),
    "qwen": ("qwen", "dashscope", "通义"),
    "moonshot": ("kimi", "moonshot"),
    "ark": ("doubao", "ark", "豆包"),
    "hunyuan": ("hunyuan", "混元"),
    "openrouter": ("openrouter",),
    "ollama": ("ollama",),
    "xai": ("grok", "xai"),
    "gemini": ("gemini",),
    "claude": ("claude", "anthropic"),
    "minimax": ("minimax", "abab"),
    "mistral": ("mistral", "codestral"),
    "baichuan": ("baichuan", "百川"),
    "step": ("step-", "阶跃"),
    "yi": ("yi-", "零一"),
    "siliconflow": ("siliconflow",),
    "openai": ("gpt", "openai", "o1", "o3", "o4", "o5", "o6", "o7", "o8", "o9"),
}


def detect_provider_name(model_name: str) -> str:
    """按模型名猜厂商（仅用于没配端点时兜底与报错提示）。"""
    lower = model_name.lower()
    for provider, keywords in PROVIDER_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return provider
    return "unknown"  # 未知模型 — 由调用方决定如何处理


def detect_base_url(model_name: str, configured_url: str = "") -> str:
    """解析端点：用户配的端点永远优先。

    优先级:
      1. configured_url 非空 → 原样返回（厂商推断不再覆盖它）
      2. 未配置 + 认出厂商且有内置端点 → 该端点兜底
      3. 都没有 → 空字符串（= 未配置）
    """
    if configured_url:
        return configured_url
    provider = detect_provider_name(model_name)
    if provider in KNOWN_BASE_URLS:
        return KNOWN_BASE_URLS[provider]
    return ""


# ── 思考参数方言（openai_provider._thinking_params 消费）──────────
# 判定顺序即优先级：OpenRouter > OpenAI 官方（这两个只认端点域名 —— Azure 等
# 代理网关跑 gpt 模型不按官方方言）> GLM / DeepSeek（认模型名，域名也算信号）。
DIALECT_OPENROUTER = "openrouter"
DIALECT_OPENAI = "openai"
DIALECT_ZHIPU = "zhipu"
DIALECT_DEEPSEEK = "deepseek"
DIALECT_GENERIC = "generic"


def dialect_of(model_name: str = "", base_url: str = "") -> str:
    """(模型名, 端点) → 思考参数方言标签。

    factory.create_provider 会算好显式传给 provider；provider 被直接构造
    （测试）时自己调这里 —— 两路都走本函数，口径只有一份。
    """
    url = (base_url or "").lower()
    name = (model_name or "").lower()
    if "openrouter" in url:
        return DIALECT_OPENROUTER
    if "api.openai.com" in url:
        return DIALECT_OPENAI
    if "glm" in name:
        return DIALECT_ZHIPU
    if "deepseek" in url or "deepseek" in name:
        return DIALECT_DEEPSEEK
    return DIALECT_GENERIC
