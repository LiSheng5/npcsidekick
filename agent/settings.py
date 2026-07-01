"""
AgentSettings — 可注入的配置数据类。

Reasonix 模式: 配置不是全局变量, 而是可注入的 Settings 对象。
这使测试可以注入不同配置, 无需修改环境变量或 monkey-patch。

config.py 保留为薄封装 — 创建默认 Settings 实例并导出属性。
新代码应接受 Settings 对象, 旧代码通过 config 属性访问不受影响。

使用:
  # 生产
  from agent.settings import AgentSettings
  settings = AgentSettings()

  # 测试
  settings = AgentSettings(max_retries=1, reflection_enabled=False)
  planner = Planner(llm, memory, settings=settings)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _default_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent  # D:\Dagent


def _default_memory_dir() -> Path:
    d = _default_base_dir() / "agent" / "memory" / "store"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_api_key() -> str:
    """按优先级加载 API Key: 环境变量 > api_key.txt 文件"""
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    if key:
        return key
    key_file = _default_base_dir() / "api_key.txt"
    if key_file.exists():
        saved = key_file.read_text("utf-8").strip()
        if saved:
            return saved
    return ""


@dataclass
class AgentSettings:
    """
    Dagent 全局配置 — 可注入, 可覆盖。

    所有字段都有合理默认值, 测试只需覆盖关心的字段。
    """

    # ── Paths ──────────────────────────────────────────
    base_dir: Path = field(default_factory=_default_base_dir)
    memory_dir: Path = field(default_factory=_default_memory_dir)
    short_term_file: Path = field(default=None)
    long_term_file: Path = field(default=None)
    notes_file: Path = field(default=None)
    api_key_file: Path = field(default=None)

    # ── LLM ────────────────────────────────────────────
    model_name: str = field(default_factory=lambda:
        os.getenv("AGENT_MODEL", "deepseek-chat"))
    provider_name: str = "auto"  # "auto" | "openai" | "deepseek"
    api_key: str = field(default_factory=_load_api_key)
    base_url: str = field(default_factory=lambda:
        os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com")
    temperature: float = 0.2
    max_tokens: int = 4096

    # ── Agent Limits ───────────────────────────────────
    max_plan_steps: int = 20
    max_retries: int = 3
    step_timeout_sec: int = 60
    max_history_items: int = 200
    max_long_term_items: int = 200

    # ── Reflection ─────────────────────────────────────
    reflection_enabled: bool = True
    reflection_use_llm: bool = True
    reflection_depth: int = 1

    # ── Vector Store ───────────────────────────────────
    vector_store_dir: Path = field(default=None)
    vector_search_top_k: int = 15
    vector_similarity_threshold: float = 0.3

    def __post_init__(self):
        # Path defaults that depend on other fields (merged — only one __post_init__ allowed)
        if self.short_term_file is None:
            self.short_term_file = self.memory_dir / "short_term.json"
        if self.long_term_file is None:
            self.long_term_file = self.memory_dir / "long_term.json"
        if self.notes_file is None:
            self.notes_file = self.base_dir / "notes.txt"
        if self.api_key_file is None:
            self.api_key_file = self.base_dir / "api_key.txt"
        if self.vector_store_dir is None:
            self.vector_store_dir = self.memory_dir / "chroma"

    # ── Context Compression ────────────────────────────
    compression_token_threshold: int = 4000
    compression_ratio: float = 0.4
    compression_min_messages: int = 20
    compression_max_response_tokens: int = 500

    # ── Logging ────────────────────────────────────────
    log_level: str = "INFO"
    log_mode: str = "console"  # "console" | "json" | "test"

    # ── Streaming ──────────────────────────────────────
    streaming_enabled: bool = False

    def to_dict(self) -> dict:
        """导出为字典 (用于序列化)。排除 Path 对象。"""
        result = {}
        for k, v in self.__dict__.items():
            if isinstance(v, Path):
                result[k] = str(v)
            else:
                result[k] = v
        return result


# ── 全局默认实例 ──────────────────────────────────────────
# config.py 使用此实例暴露旧式属性访问

_default_settings: Optional[AgentSettings] = None


def get_settings() -> AgentSettings:
    """获取全局默认 Settings 实例 (惰性创建)。"""
    global _default_settings
    if _default_settings is None:
        _default_settings = AgentSettings()
    return _default_settings


def reset_settings() -> None:
    """重置全局 Settings (测试用)。"""
    global _default_settings
    _default_settings = None
