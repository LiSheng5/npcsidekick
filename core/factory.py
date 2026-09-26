"""
Provider Factory — 按用户配置创建 Provider（模型 / key / 端点三件套）。

v4 不绑定任何厂商：谁来当大脑由 `AGENT_MODEL` + `NPC_API_KEY` + `NPC_BASE_URL` 决定。
国内外主流厂商（含本地 Ollama / vLLM）基本都提供 OpenAI 兼容端点，
故所有 Provider 统一走 OpenAIProvider（Anthropic 原生 API 不兼容 OpenAI 格式，
需走聚合网关或自建兼容层）。

端点解析优先级（**配置优先**）:
  1. 显式给了 base_url → **直接用**，不被任何厂商推断覆盖
    （这条是接 OpenRouter / 自建网关 / 代理时唯一生效的规则）
  2. 没给 base_url，但模型名能认出厂商且该厂商有内置端点 → 用内置端点兜底
  3. 都没认出来 → 视为"未配置"，由 resolve_llm_config 报缺哪一项

模型名关键词只用于 ① 没配端点时的兜底 ② 报错时提示"看起来是哪家"。
注释里的端点地址以厂商最新文档为准 —— 想确定就显式配 NPC_BASE_URL。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core.base import ProviderProtocol
from core.openai_provider import OpenAIProvider


# 已知 provider 的默认 base_url（仅在用户没配 NPC_BASE_URL 时兜底）
_KNOWN_BASE_URLS: dict[str, str] = {
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
_PROVIDER_KEYWORDS: dict[str, tuple[str, ...]] = {
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

# 缺某一项时给用户的提示文案（响亮失败：说清该配什么）
_CONFIG_HINTS: dict[str, str] = {
    "NPC_API_KEY": "NPC_API_KEY（或旧名 DEEPSEEK_API_KEY / OPENAI_API_KEY / ZHIPU_API_KEY）",
    "AGENT_MODEL": "AGENT_MODEL（或 NPC_MODEL）—— 模型名由用户自定",
    "NPC_BASE_URL": "NPC_BASE_URL（OpenAI 兼容端点，见厂商文档）",
}


def detect_provider_name(model_name: str) -> str:
    """按模型名猜厂商（仅用于没配端点时兜底与报错提示）。"""
    lower = model_name.lower()
    for provider, keywords in _PROVIDER_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return provider
    return "unknown"  # 未知模型 — 由调用方决定如何处理


def detect_base_url(model_name: str, configured_url: str = "") -> str:
    """
    解析端点：用户配的端点永远优先。

    优先级:
      1. configured_url 非空 → 原样返回（厂商推断不再覆盖它）
      2. 未配置 + 认出厂商且有内置端点 → 该端点兜底
      3. 都没有 → 空字符串（= 未配置）
    """
    if configured_url:
        return configured_url
    provider = detect_provider_name(model_name)
    if provider in _KNOWN_BASE_URLS:
        return _KNOWN_BASE_URLS[provider]
    return ""


def hint_list(missing: List[str]) -> List[str]:
    """把缺的配置项翻译成可直接照抄的提示。"""
    return [_CONFIG_HINTS.get(name, name) for name in missing]


def resolve_llm_config(api_key: str, model_name: str, base_url: str = "") -> Dict[str, Any]:
    """解析 LLM 三件套，返回 {model, base_url, ready, missing, source}。

    ready=False 时 missing 列出缺哪几项（供 /api/state 与日志响亮提示）；
    此时服务照常起，`/api/talk` 走角色卡 rules 兜底回复、不提议动作。
    source: "configured"（用户显式配端点）/ "inferred"（按模型名兜底）/ ""（没有）
    """
    missing: List[str] = []
    if not api_key:
        missing.append("NPC_API_KEY")
    if not model_name:
        missing.append("AGENT_MODEL")

    source = ""
    if base_url:
        source = "configured"
    else:
        inferred = detect_base_url(model_name, "")
        if inferred:
            base_url = inferred
            source = "inferred"
        else:
            missing.append("NPC_BASE_URL")

    return {
        "model": model_name or None,
        "base_url": base_url or None,
        "ready": not missing,
        "missing": missing,
        "source": source,
    }


def current_llm_config() -> Dict[str, Any]:
    """现读环境变量解析三件套（端点与模型名每次调用现读，可热切）。"""
    from core import config

    model = os.environ.get("AGENT_MODEL") or os.environ.get("NPC_MODEL") or config.MODEL_NAME
    base = os.environ.get("NPC_BASE_URL") or config.BASE_URL
    return resolve_llm_config(api_key=config.API_KEY or "", model_name=model, base_url=base)


def create_provider(
    api_key: str,
    model_name: str,
    base_url: str = "",
    temperature: float = 0.2,
    max_tokens: int = 8192,
    provider_name: str = "auto",
    extra_body: Optional[dict] = None,
) -> ProviderProtocol:
    """
    创建 Provider 实例。

    Args:
      api_key: API 密钥
      model_name: 模型名 (用户自定，如 "deepseek-v4-pro" / "qwen-plus" / "gpt-5")
      base_url: OpenAI 兼容端点 (用户自配；留空则按模型名兜底)
      temperature: 温度参数
      max_tokens: 最大 token 数
      provider_name: "auto" | "openai" | "deepseek" | ... — 仅在没配 base_url 时生效
      extra_body: 额外请求体片段 (也可用环境变量 NPC_LLM_EXTRA_BODY 现读)

    Returns:
      ProviderProtocol 实例

    示例:
      # 自配端点（推荐，接任何厂商/网关/本地模型）
      p = create_provider("sk-...", "deepseek-v4-pro", base_url="https://openrouter.ai/api/v1")

      # 没配端点时按模型名兜底
      p = create_provider("sk-...", "deepseek-v4-pro")   # → api.deepseek.com

      # 本地 Ollama
      p = create_provider("ollama", "qwen3:8b", base_url="http://localhost:11434/v1")
    """
    if provider_name != "auto" and not base_url and provider_name in _KNOWN_BASE_URLS:
        base_url = _KNOWN_BASE_URLS[provider_name]
    base_url = detect_base_url(model_name, base_url)

    if not base_url:
        provider = detect_provider_name(model_name)
        known = f"（模型名看起来是 {provider}，但 v4 没有内置该厂商端点）" \
            if provider != "unknown" else "（模型名认不出厂商）"
        raise ValueError(
            f"无法确定模型 '{model_name}' 的 API 端点{known}。"
            f"请设置环境变量 NPC_BASE_URL（OpenAI 兼容端点）。"
        )

    # 所有已知 provider 都使用 OpenAI 兼容 API
    return OpenAIProvider(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )
