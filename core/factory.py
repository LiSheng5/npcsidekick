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

from typing import Any, Dict, List

from core.base import ProviderProtocol
from core.openai_provider import OpenAIProvider
from core.settings import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from core.vendor import (
    detect_base_url,
    detect_provider_name,
    dialect_of,
)

# 厂商探测（模型名关键词 / 内置端点 / 思考参数方言）单一来源在 core/vendor.py；
# 这里 import 后 detect_provider_name / detect_base_url 继续以 factory.* 可用（既有调用方口径不变）。

# 缺某一项时给用户的提示文案（响亮失败：说清该配什么）
_CONFIG_HINTS: dict[str, str] = {
    "NPC_API_KEY": "NPC_API_KEY（或旧名 DEEPSEEK_API_KEY / OPENAI_API_KEY / ZHIPU_API_KEY）",
    "AGENT_MODEL": "AGENT_MODEL（或 NPC_MODEL）—— 模型名由用户自定",
    "NPC_BASE_URL": "NPC_BASE_URL（OpenAI 兼容端点，见厂商文档）",
}


def hint_list(missing: List[str]) -> List[str]:
    """把缺的配置项翻译成可直接照抄的提示。"""
    return [_CONFIG_HINTS.get(name, name) for name in missing]


def resolve_llm_config(api_key: str, model_name: str, base_url: str = "") -> Dict[str, Any]:
    """解析 LLM 三件套，返回 {model, base_url, ready, missing, source}。

    ready=False 时 missing 列出缺哪几项（供 /api/state 与日志响亮提示）；
    此时服务照常起，`/api/talk` 走角色卡 rules 兜底回复、不调用动作。
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
    """现读环境变量解析三件套（端点与模型名每次现读，可热切）。

    env 回退链只有一份（core/settings.AgentSettings 的字段默认值），
    这里直接走 core.config，不再自己 or 一遍。
    """
    from core import config

    return resolve_llm_config(api_key=config.api_key() or "",
                              model_name=config.model_name(),
                              base_url=config.base_url())


def create_provider(
    api_key: str,
    model_name: str,
    base_url: str = "",
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ProviderProtocol:
    """
    创建 Provider 实例。

    Args:
      api_key: API 密钥
      model_name: 模型名 (用户自定，如 "deepseek-v4-pro" / "qwen-plus" / "gpt-5")
      base_url: OpenAI 兼容端点 (用户自配；留空则按模型名兜底)
      temperature: 温度参数
      max_tokens: 最大 token 数

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
    base_url = detect_base_url(model_name, base_url)

    if not base_url:
        provider = detect_provider_name(model_name)
        known = f"（模型名看起来是 {provider}，但 v4 没有内置该厂商端点）" \
            if provider != "unknown" else "（模型名认不出厂商）"
        raise ValueError(
            f"无法确定模型 '{model_name}' 的 API 端点{known}。"
            f"请设置环境变量 NPC_BASE_URL（OpenAI 兼容端点）。"
        )

    # 所有已知 provider 都使用 OpenAI 兼容 API；思考参数方言由 core/vendor 判定后
    # 显式传给 provider（provider 不再按域名自己猜一遍）
    return OpenAIProvider(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        dialect=dialect_of(model_name, base_url),
    )
