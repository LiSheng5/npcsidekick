"""
运行路径真正读取的配置项 —— **每次现读**环境变量。

家规（见 `core/config_flags.py` 抬头）：默认关 + 每次现读可热切，**绝不做 import 时缓存**。
所以这里只提供函数、不提供模块常量：`config.api_key()` 每次都用当刻的环境变量取，
改环境变量后无需重启。代价是每次调用重建一个 `AgentSettings`（纯读环境变量，可忽略）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from core.config_flags import env_flag, env_num, env_text, warn_once
from core.settings import AgentSettings

# 上下文压缩（窗口 × 比例触发）默认值；环境变量每次现读可热切
_DEFAULT_CONTEXT_WINDOW = 300_000
_MIN_CONTEXT_WINDOW = 8_000
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


def valid_context_window(value: Any) -> Optional[int]:
    """整数且 ≥ 8000 才合法，否则 None。可无损转换的字符串也认。"""
    if isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if not text or str(int(text)) != text:
            return None
        number = int(text)
    except (TypeError, ValueError):
        return None
    return number if number >= _MIN_CONTEXT_WINDOW else None


def env_context_window() -> int:
    """环境变量层的窗口：NPC_CONTEXT_WINDOW，默认 300000。

    非法或低于 8000 → 回落默认并告警一次。llm_runtime 的环境层起点走这里
    （**不要**在它里面调 context_window() —— 那会经 llm_settings 绕回来）。
    """
    raw = env_text(_ENV_CONTEXT_WINDOW, "").strip()
    if not raw:
        return _DEFAULT_CONTEXT_WINDOW
    number = valid_context_window(raw)
    if number is None:
        warn_once(_ENV_CONTEXT_WINDOW, "env_value_invalid_fallback", raw[:40],
                  raw=raw[:40], fallback=str(_DEFAULT_CONTEXT_WINDOW),
                  min=_MIN_CONTEXT_WINDOW)
        return _DEFAULT_CONTEXT_WINDOW
    return number


def context_window(store_dir: Optional[Path] = None) -> int:
    """当前模型的上下文窗口（token）—— 三层合并：页面 > store/llm_config.json >
    环境变量（NPC_CONTEXT_WINDOW），默认 300000。换模型要同步改，
    否则「提示用到窗口的 40% 就压缩」会按错的窗口算比例。

    页面在控制台「模型」页改、落 llm_config.json，改值不重启即生效。
    合并结果非法（手改文件写坏等）→ 回落默认并告警一次。
    函数内惰性 import llm_runtime：它的环境层反过来读本模块，避免循环依赖。
    """
    from core import llm_runtime

    raw = llm_runtime.llm_settings(store_dir=store_dir).get("context_window")
    number = valid_context_window(raw)
    if number is None:
        warn_once("context_window", "value_invalid_fallback", str(raw)[:40],
                  raw=str(raw)[:40], fallback=str(_DEFAULT_CONTEXT_WINDOW),
                  min=_MIN_CONTEXT_WINDOW)
        return _DEFAULT_CONTEXT_WINDOW
    return number


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
# 默认值：state 800、memory 2000（记忆块在最后一条 user 里、不进前缀缓存）；
# history 不设固定默认 —— 硬上限 = 上下文窗口 × 0.7（HISTORY_BUDGET_RATIO）。
DEFAULT_BUDGET_STATE = 800
DEFAULT_BUDGET_MEMORY = 2000
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


def budget_history(store_dir: Optional[Path] = None) -> int:
    """对话层硬上限（token），现读环境变量，经 build_history(token_budget=...) 生效。

    未显式设置 `NPC_BUDGET_HISTORY` 时 = 上下文窗口 × 0.7（现算，窗口取三层合并值）。
    正常不会触发：历史 = 摘要 + 未被摘要覆盖的全部聊天记录，提示用到窗口的
    NPC_SUMMARY_RATIO（默认 40%）就压缩、只留最近几轮；这个硬上限只防摘要
    失败（LLM 不可用）时历史无限涨、把上下文撑爆。
    """
    default = int(context_window(store_dir) * HISTORY_BUDGET_RATIO)
    return max(0, int(env_num(_ENV_BUDGET_HISTORY, default, cast=int)))


# ── 玩家行为库（事件层）──────────────────────────────────
# 两类事件：npc_interactions（玩家对这个 NPC 的互动，回合推送进提示词）与
# player_acts（玩家一般行为，check_player_log 按可见性拉取）。这里只放
# 预算与窗口的读取函数（events.py 现读调用，服务端/turn 侧也各取所需）；
# 全部现读可热切，与上面各层预算同一套家规。
DEFAULT_BUDGET_EVENTS = 600
DEFAULT_EVENT_PUSH_MAX = 8
DEFAULT_EVENT_RECENT_MAX = 5
DEFAULT_EVENT_RECENT_MINUTES = 30.0
DEFAULT_PLAYER_LOG_DIGEST_MINUTES = 120.0
DEFAULT_EVENT_MERGE_SECONDS = 60.0
DEFAULT_EVENTS_MAX = 5000
DEFAULT_EVENTS_KEEP_DAYS = 30.0

_ENV_PLAYER_LOG = "NPC_PLAYER_LOG"
_ENV_BUDGET_EVENTS = "NPC_BUDGET_EVENTS"
_ENV_EVENT_PUSH_MAX = "NPC_EVENT_PUSH_MAX"
_ENV_EVENT_RECENT_MAX = "NPC_EVENT_RECENT_MAX"
_ENV_EVENT_RECENT_MINUTES = "NPC_EVENT_RECENT_MINUTES"
_ENV_PLAYER_LOG_DIGEST_MINUTES = "NPC_PLAYER_LOG_DIGEST_MINUTES"
_ENV_EVENT_MERGE_SECONDS = "NPC_EVENT_MERGE_SECONDS"
_ENV_EVENTS_MAX = "NPC_EVENTS_MAX"
_ENV_EVENTS_KEEP_DAYS = "NPC_EVENTS_KEEP_DAYS"


def player_log_enabled() -> bool:
    """玩家行为库总开关（现读，可热切）：NPC_PLAYER_LOG，默认开。

    关 = 不注入事件块/目录行、不给 check_player_log 工具；/api/events 照收。
    布尔词表沿用现有口径（1/true/yes/on = 开）。
    """
    return env_flag(_ENV_PLAYER_LOG, True)


def budget_events() -> int:
    """【玩家对你做了什么】块 token 预算（独立预算，不占 state）；负数钳到 0。"""
    return max(0, int(env_num(_ENV_BUDGET_EVENTS, DEFAULT_BUDGET_EVENTS, cast=int)))


def event_push_max() -> int:
    """推送块「新事」条数上限：NPC_EVENT_PUSH_MAX，默认 8。"""
    return max(0, int(env_num(_ENV_EVENT_PUSH_MAX, DEFAULT_EVENT_PUSH_MAX, cast=int)))


def event_recent_max() -> int:
    """推送块「近事」条数上限：NPC_EVENT_RECENT_MAX，默认 5。"""
    return max(0, int(env_num(_ENV_EVENT_RECENT_MAX, DEFAULT_EVENT_RECENT_MAX, cast=int)))


def event_recent_minutes() -> float:
    """「近事」时间窗（分钟）：NPC_EVENT_RECENT_MINUTES，默认 30。"""
    return max(0.0, env_num(_ENV_EVENT_RECENT_MINUTES, DEFAULT_EVENT_RECENT_MINUTES))


def player_log_digest_minutes() -> float:
    """目录行（【玩家行为记录】）统计窗（分钟）：NPC_PLAYER_LOG_DIGEST_MINUTES，默认 120。"""
    return max(0.0, env_num(_ENV_PLAYER_LOG_DIGEST_MINUTES,
                            DEFAULT_PLAYER_LOG_DIGEST_MINUTES))


def event_merge_seconds() -> float:
    """事件合并窗口（秒）：NPC_EVENT_MERGE_SECONDS，默认 60。"""
    return max(0.0, env_num(_ENV_EVENT_MERGE_SECONDS, DEFAULT_EVENT_MERGE_SECONDS))


def events_max() -> int:
    """每表条数上限：NPC_EVENTS_MAX，默认 5000（severity>=8 豁免）。"""
    return max(1, int(env_num(_ENV_EVENTS_MAX, DEFAULT_EVENTS_MAX, cast=int)))


def events_keep_days() -> float:
    """事件保留天数：NPC_EVENTS_KEEP_DAYS，默认 30。"""
    return max(0.0, env_num(_ENV_EVENTS_KEEP_DAYS, DEFAULT_EVENTS_KEEP_DAYS))
