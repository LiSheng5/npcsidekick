"""
AgentSettings — 可注入的配置数据类（只保留运行路径真正用到的字段）。

⚠️ 刻意**不做单例、不做缓存**：`core/config.py` 的每个访问函数都会新建一个实例，
保证「改环境变量无需重启」的家规（见 `core/config_flags.py` 抬头）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from core.config_flags import env_text


@lru_cache(maxsize=1)
def _default_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent  # 项目根目录


def _load_api_key() -> str:
    """按优先级加载 API Key: 环境变量 > api_key.txt 文件

    通用名 NPC_API_KEY 优先（v4 不绑厂商）；DEEPSEEK_* / OPENAI_* / ZHIPU_* 是旧名，
    仅为兼容保留。
    """
    key = (env_text("NPC_API_KEY")
           or env_text("DEEPSEEK_API_KEY")
           or env_text("OPENAI_API_KEY")
           or env_text("ZHIPU_API_KEY"))
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
        if env_text(name):
            return name
    if (_default_base_dir() / "api_key.txt").exists():
        return "api_key.txt"
    return ""


@dataclass
class AgentSettings:
    """
    NPCSidekick 配置 — 纯读环境变量，可注入、可覆盖。

    「三件套」全部由用户自配，v4 不预设厂商：模型名 / key / 端点任缺一项 → 没有大脑
    （`/api/talk` 走角色卡 rules 兜底回复，不调用动作）。
    """

    # ── LLM（三件套 + 采样参数）─────────────────────────
    model_name: str = field(default_factory=lambda:
        env_text("AGENT_MODEL") or env_text("NPC_MODEL") or "")
    api_key: str = field(default_factory=_load_api_key)
    base_url: str = field(default_factory=lambda:
        env_text("NPC_BASE_URL")
        or env_text("DEEPSEEK_BASE_URL")
        or env_text("OPENAI_BASE_URL")
        or "")
    temperature: float = 0.2
    max_tokens: int = 8192
