"""
运行路径真正读取的配置项 —— **每次现读**环境变量。

家规（见 `core/config_flags.py` 抬头）：默认关 + 每次现读可热切，**绝不做 import 时缓存**。
所以这里只提供函数、不提供模块常量：`config.api_key()` 每次都用当刻的环境变量取，
改环境变量后无需重启。代价是每次调用重建一个 `AgentSettings`（纯读环境变量，可忽略）。
"""
from __future__ import annotations

from core.config_flags import env_num, warn_once
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
    """提示用到上下文窗口的这个比例就触发压缩。NPC_SUMMARY_RATIO，钳在 [0, 1]。

    超出范围钳到边界（非法取值回落由 env_num 告警），钳制同样告警一次，
    不静默 —— 用户设了 2 却按 1 跑，得在日志里看得见。
    """
    ratio = float(env_num(_ENV_SUMMARY_RATIO, _DEFAULT_SUMMARY_RATIO))
    if ratio < 0.0 or ratio > 1.0:
        clamped = min(1.0, max(0.0, ratio))
        warn_once(_ENV_SUMMARY_RATIO, "env_value_clamped", str(ratio),
                  raw=str(ratio), clamped=str(clamped), valid_range="[0, 1]")
        return clamped
    return ratio


def summary_keep_recent() -> int:
    """压缩后仍逐字保留的最近轮数。NPC_SUMMARY_KEEP_RECENT，默认 3。"""
    return max(0, int(env_num(_ENV_SUMMARY_KEEP_RECENT, _DEFAULT_SUMMARY_KEEP_RECENT,
                              cast=int)))


# ── 上下文各层预算（装配层 context.assemble 的入参，由 turn.py 读好再传）──
# 预算读取放这里（而不是 context.py）的理由：它们是环境变量读取项，与
# context「纯函数、不读环境变量」的自我声明冲突；chatlog.build_history 的
# 硬上限兜底也要用它 —— 放 core 后 chatlog 不必反向依赖装配层。
# 默认值：state 800、memory 600；history 不设固定默认 ——
# 硬上限 = 上下文窗口 × 0.7（HISTORY_BUDGET_RATIO）。
DEFAULT_BUDGET_STATE = 800
DEFAULT_BUDGET_MEMORY = 600
HISTORY_BUDGET_RATIO = 0.7

_ENV_BUDGET_STATE = "NPC_BUDGET_STATE"
_ENV_BUDGET_MEMORY = "NPC_BUDGET_MEMORY"
_ENV_BUDGET_HISTORY = "NPC_BUDGET_HISTORY"


def budget_state() -> int:
    """观测块预算（token），现读环境变量；负数钳到 0。"""
    return max(0, int(env_num(_ENV_BUDGET_STATE, DEFAULT_BUDGET_STATE, cast=int)))


def budget_memory() -> int:
    """记忆块预算（token），现读环境变量（记忆预取用）；负数钳到 0。"""
    return max(0, int(env_num(_ENV_BUDGET_MEMORY, DEFAULT_BUDGET_MEMORY, cast=int)))


def budget_history() -> int:
    """对话层硬上限（token），现读环境变量，经 build_history(token_budget=...) 生效。

    未显式设置 `NPC_BUDGET_HISTORY` 时 = 上下文窗口 × 0.7（现算）。
    正常不会触发：历史 = 摘要 + 未被摘要覆盖的全部聊天记录，提示用到窗口的
    NPC_SUMMARY_RATIO（默认 40%）就压缩、只留最近几轮；这个硬上限只防摘要
    失败（LLM 不可用）时历史无限涨、把上下文撑爆。
    """
    default = int(context_window() * HISTORY_BUDGET_RATIO)
    return max(0, int(env_num(_ENV_BUDGET_HISTORY, default, cast=int)))
