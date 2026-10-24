"""NPC_* 环境变量统一读取器（P1-2·2026-08-25）。

家规契约: 默认关 + 每次现读可热切 —— 本模块只提供函数, 绝不做 import 时缓存
（`_WARNED` 只用于非法值告警去重, 不缓存任何配置值）。
布尔词表统一: ON = {1, true, yes, on}(大小写/空白不敏感), 其余一律 OFF。
  · 修复旧宽松派 bug: NPC_SCHEDULER=false 曾被当成开启。
已知特例(暂不迁移): subagent.py 的 总闸==0 + 单开关==1 双键语义(见该模块)。
"""
from __future__ import annotations

import os

from core.logging_config import log

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# 同一告警只发一次的登记表：env 每次现读，不去重会把同一条 warning 刷满日志。
# 键 = (env 名, 事件, 触发值) —— 换个值还会再提醒，同一坏值只提醒一次。
_WARNED: set = set()


def warn_once(env: str, event: str, trigger: str, **fields) -> None:
    """坏 env 取值的告警（同 env + 同事件 + 同触发值只发一次）。"""
    tag = (env, event, trigger)
    if tag in _WARNED:
        return
    _WARNED.add(tag)
    log.warning(event, env=env, **fields)


def env_flag(name: str, default: bool = False) -> bool:
    """布尔开关: 未设置→default; 设置则按词表判定(大小写/空白不敏感)。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def env_num(name: str, default, cast=float):
    """数值开关: 未设置/空 → default(经 cast); 非法 → default 并告警一次; 合法 → cast(原值)。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return cast(default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        warn_once(name, "env_num_invalid_fallback", str(raw)[:40],
                  raw=str(raw)[:40], fallback=str(cast(default)))
        return cast(default)


def env_text(name: str, default: str = "") -> str:
    """文本项: 未设置 → default; 设置 → 原样返回(与 os.environ.get 同语义, 不 strip)。"""
    raw = os.environ.get(name)
    return default if raw is None else raw