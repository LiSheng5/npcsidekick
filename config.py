"""
全局配置 — Dagent 项目的共享设置入口。

此文件是 AgentSettings 的薄封装：
  import config; config.MAX_RETRIES

测试可创建独立的 AgentSettings 实例，无需修改全局状态。
"""

from agent.settings import AgentSettings, get_settings, reset_settings as _reset_settings

# ── 惰性单例 ──────────────────────────────────────────────
_settings: AgentSettings = None  # type: ignore[assignment]


def _get() -> AgentSettings:
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


def reload_config():
    """重新加载配置 (测试或运行时切换设置后调用)。"""
    global _settings
    _settings = None
    _refresh_module_attrs()


def _refresh_module_attrs():
    """刷新模块级属性 (reload_config 后调用)。"""
    s = _get()
    for name in dir(s):
        if not name.startswith("_"):
            val = getattr(s, name)
            globals()[name.upper()] = val


# ── 初始化模块属性 ────────────────────────────────────────
# 从默认 Settings 同步所有值到模块命名空间
_s = _get()
_base_url_val = _s.base_url
_model_name_val = _s.model_name
_api_key_val = _s.api_key

# Paths
BASE_DIR            = _s.base_dir
MEMORY_DIR          = _s.memory_dir
SHORT_TERM_FILE     = _s.short_term_file
LONG_TERM_FILE      = _s.long_term_file
NOTES_FILE          = _s.notes_file
API_KEY_FILE        = _s.api_key_file

# LLM
MODEL_NAME    = _s.model_name
PROVIDER_NAME = _s.provider_name
API_KEY       = _s.api_key
BASE_URL      = _s.base_url
TEMPERATURE   = _s.temperature
MAX_TOKENS        = _s.max_tokens
REASONING_EFFORT  = _s.reasoning_effort

# Agent Limits
MAX_PLAN_STEPS     = _s.max_plan_steps
MAX_RETRIES        = _s.max_retries
STEP_TIMEOUT_SEC   = _s.step_timeout_sec
MAX_HISTORY_ITEMS  = _s.max_history_items
MAX_LONG_TERM_ITEMS = _s.max_long_term_items

# Reflection
REFLECTION_ENABLED  = _s.reflection_enabled
REFLECTION_USE_LLM  = _s.reflection_use_llm
REFLECTION_DEPTH    = _s.reflection_depth

# Vector Store
VECTOR_STORE_DIR              = _s.vector_store_dir
VECTOR_SEARCH_TOP_K           = _s.vector_search_top_k
VECTOR_SIMILARITY_THRESHOLD   = _s.vector_similarity_threshold

# Context Compression
COMPRESSION_TOKEN_THRESHOLD     = _s.compression_token_threshold
COMPRESSION_RATIO               = _s.compression_ratio
COMPRESSION_MIN_MESSAGES        = _s.compression_min_messages
COMPRESSION_MAX_RESPONSE_TOKENS = _s.compression_max_response_tokens

# Logging
LOG_LEVEL = _s.log_level
LOG_MODE  = _s.log_mode
