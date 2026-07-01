"""
全局配置 — Dagent 项目的共享设置入口。
"""
import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
MEMORY_DIR = BASE_DIR / "agent" / "memory" / "store"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
SHORT_TERM_FILE = MEMORY_DIR / "short_term.json"
LONG_TERM_FILE  = MEMORY_DIR / "long_term.json"
NOTES_FILE      = BASE_DIR / "notes.txt"

# ── LLM ───────────────────────────────────────────────
MODEL_NAME   = os.getenv("AGENT_MODEL", "deepseek-chat")
API_KEY      = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
BASE_URL     = os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com"
TEMPERATURE  = 0.2
MAX_TOKENS   = 4096

# ── Agent limits ──────────────────────────────────────
MAX_PLAN_STEPS    = 20       # 计划的最大步骤数
MAX_RETRIES       = 3        # 单步最大重试次数
STEP_TIMEOUT_SEC  = 60       # 单步执行超时（秒）
MAX_HISTORY_ITEMS = 200      # 短时记忆最大条数
MAX_LONG_TERM_ITEMS = 200    # 长时记忆最大条数

# ── Reflection ────────────────────────────────────────
REFLECTION_ENABLED = True    # 是否启用反射/自校正
REFLECTION_DEPTH   = 1       # 反射深度（>1 会嵌套反射）