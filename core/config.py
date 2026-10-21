"""
运行路径真正读取的配置项 —— **每次现读**环境变量。

家规（见 `core/config_flags.py` 抬头）：默认关 + 每次现读可热切，**绝不做 import 时缓存**。
所以这里只提供函数、不提供模块常量：`config.api_key()` 每次都用当刻的环境变量取，
改环境变量后无需重启。代价是每次调用重建一个 `AgentSettings`（纯读环境变量，可忽略）。
"""
from __future__ import annotations

from core.config_flags import env_num
from core.settings import AgentSettings

# 上下文压缩（窗口 × 比例触发）默认值；环境变量每次现读可热切
_DEFAULT_CONTEXT_WINDOW = 1_000_000
_DEFAULT_SUMMARY_RATIO = 0.4
_DEFAULT_SUMMARY_KEEP_RECENT = 3

_ENV_CONTEXT_WINDOW = "NPC_CONTEXT_WINDOW"
_ENV_SUMMARY_RATIO = "NPC_SUMMARY_RATIO"
_ENV_SUMMARY_KEEP_RECENT = "NPC_SUMMARY_KEEP_RECENT"


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


def context_window() -> int:
    """当前模型的上下文窗口（token）。NPC_CONTEXT_WINDOW —— 换模型要同步改，
    否则「提示用到窗口的 40% 就压缩」会按错的窗口算比例。"""
    return max(1, int(env_num(_ENV_CONTEXT_WINDOW, _DEFAULT_CONTEXT_WINDOW, cast=int)))


def summary_ratio() -> float:
    """提示用到上下文窗口的这个比例就触发压缩。NPC_SUMMARY_RATIO，钳在 [0, 1]。"""
    ratio = float(env_num(_ENV_SUMMARY_RATIO, _DEFAULT_SUMMARY_RATIO))
    return min(1.0, max(0.0, ratio))


def summary_keep_recent() -> int:
    """压缩后仍逐字保留的最近轮数。NPC_SUMMARY_KEEP_RECENT，默认 3。"""
    return max(0, int(env_num(_ENV_SUMMARY_KEEP_RECENT, _DEFAULT_SUMMARY_KEEP_RECENT,
                              cast=int)))
