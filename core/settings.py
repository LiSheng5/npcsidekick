"""
AgentSettings — 可注入的配置数据类。

使用:
  from agent.settings import AgentSettings
  settings = AgentSettings()

  # 测试可注入不同配置
  settings = AgentSettings(max_retries=1, reflection_enabled=False)
  planner = Planner(llm, memory, settings=settings)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _default_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent  # 项目根目录


def _default_memory_dir() -> Path:
    d = _default_base_dir() / "store"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_api_key() -> str:
    """按优先级加载 API Key: 环境变量 > api_key.txt 文件

    通用名 NPC_API_KEY 优先（v4 不绑厂商）；DEEPSEEK_* / OPENAI_* / ZHIPU_* 是旧名，
    仅为兼容保留。
    """
    key = (os.getenv("NPC_API_KEY")
           or os.getenv("DEEPSEEK_API_KEY")
           or os.getenv("OPENAI_API_KEY")
           or os.getenv("ZHIPU_API_KEY")
           or "")
    if key:
        return key
    key_file = _default_base_dir() / "api_key.txt"
    if key_file.exists():
        saved = key_file.read_text("utf-8").strip()
        if saved:
            return saved
    return ""


def api_key_source() -> str:
    """当前 key 来自哪个环境变量 / 文件 —— 只报来源，绝不报内容。"""
    for name in ("NPC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ZHIPU_API_KEY"):
        if os.getenv(name):
            return name
    if (_default_base_dir() / "api_key.txt").exists():
        return "api_key.txt"
    return ""


@dataclass
class AgentSettings:
    """
    NPCSidekick 全局配置 — 可注入, 可覆盖。

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
    # 三件套全部由用户自配，v4 不预设厂商：模型名 / key / 端点任缺一项 → 没有大脑
    # （/api/talk 走角色卡 rules 兜底回复，不提议动作）。
    model_name: str = field(default_factory=lambda:
        os.getenv("AGENT_MODEL") or os.getenv("NPC_MODEL") or "")
    provider_name: str = "auto"  # "auto" | "openai" | "deepseek" | ...
    api_key: str = field(default_factory=_load_api_key)
    base_url: str = field(default_factory=lambda:
        os.getenv("NPC_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "")
    temperature: float = 0.2
    max_tokens: int = 8192
    reasoning_effort: str | None = None  # None | "low" | "medium" | "high" | "max"

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
