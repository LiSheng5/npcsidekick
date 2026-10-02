"""
运行路径真正读取的配置项 —— **每次现读**环境变量。

家规（见 `core/config_flags.py` 抬头）：默认关 + 每次现读可热切，**绝不做 import 时缓存**。
所以这里只提供函数、不提供模块常量：`config.api_key()` 每次都用当刻的环境变量取，
改环境变量后无需重启。代价是每次调用重建一个 `AgentSettings`（纯读环境变量，可忽略）。
"""
from __future__ import annotations

from core.settings import AgentSettings


def model_name() -> str:
    """模型名（AGENT_MODEL / NPC_MODEL）。"""
    return AgentSettings().model_name


def api_key() -> str:
    """API Key（NPC_API_KEY 等环境变量 > 工程根 api_key.txt）。"""
    return AgentSettings().api_key


def base_url() -> str:
    """OpenAI 兼容端点（NPC_BASE_URL 等）；空 = 未配置，按模型名猜厂商兜底。"""
    return AgentSettings().base_url


def temperature() -> float:
    """采样温度。"""
    return AgentSettings().temperature


def max_tokens() -> int:
    """单次回复的 token 上限。"""
    return AgentSettings().max_tokens
