"""NPC_* 环境变量统一读取器（P1-2·2026-08-25）。

家规契约: 默认关 + 每次现读可热切 —— 本模块只提供函数, 绝不做 import 时缓存。
布尔词表统一: ON = {1, true, yes, on}(大小写/空白不敏感), 其余一律 OFF。
  · 修复旧宽松派 bug: NPC_SCHEDULER=false 曾被当成开启。
已知特例(暂不迁移): subagent.py 的 总闸==0 + 单开关==1 双键语义(见该模块)。
"""
from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str, default: bool = False) -> bool:
    """布尔开关: 未设置→default; 设置则按词表判定(大小写/空白不敏感)。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def env_num(name: str, default, cast=float):
    """数值开关: 未设置/空/非法 → default(经 cast); 合法 → cast(原值)。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return cast(default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return cast(default)