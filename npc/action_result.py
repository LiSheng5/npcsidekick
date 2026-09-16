"""行动后果的结构化契约（G3 · 2026-09-16）。

背景（《NPC大脑架构》§29.3 G3，实测依据 `docs/NPC_RUNTIME_AUDIT.md` Q7）：
`world.apply_action` 的返回是 `(world, ok, 中文消息)` —— 目标进度推进、关系变化、
benchmark 指标都拿不到**可编程的信号**（没有 error_code / world_changes / duration）。

本模块在 `apply_action` **外面**包一层薄包装：
  - **不改旧签名**（`apply_action` 原样返回三件套，调用方零改动）；
  - **不改行为**（只做 before/after 快照差分 + 计时；不用它 = 等于不存在）；
  - `error_code` 只在能**可靠匹配既有失败消息**时给出（表见 `_ERROR_CODES`），
    匹配不到就留 `None` —— **宁可留空也不猜**（诚实边界，不造数据）。

用法：
    from npc.action_result import apply_action_structured
    world, result = apply_action_structured(world, "gather", {"resource": "木材"}, who="cang")
    if not result.success:
        log.warning("npc_action_failed", npc=result.who, code=result.error_code)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from npc.world import apply_action, stamina_of

# 失败消息 → 稳定 error_code。**只做锚定匹配**（前缀/后缀），不猜语义。
# 消息来源：npc/world.py::apply_action 的各 return 分支（改那边的文案时这张表要同步）。
_ERROR_CODES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"^未知行动: "), "unknown_action"),
    (re.compile(r"^无法从.+前往"), "unreachable"),
    (re.compile(r"没有资源 "), "resource_absent"),
    (re.compile(r"已采尽$"), "resource_depleted"),
    (re.compile(r"^没有配方: "), "recipe_missing"),
    (re.compile(r"^工作台在"), "wrong_place"),
    (re.compile(r"^材料不足: "), "ingredients_missing"),
    (re.compile(r"^背包里没有"), "not_carried"),
    (re.compile(r"无法交付$"), "protagonist_absent"),
    (re.compile(r"参数错误$"), "bad_params"),
)


@dataclass(frozen=True)
class ActionResult:
    """一次行动的后果（客观字段 + 中文消息原样带上）。"""

    action: str
    who: str
    success: bool
    message: str                      # apply_action 的原话（不加工）
    error_code: Optional[str] = None  # 只在可靠匹配时给出，否则 None
    world_changes: Dict[str, Any] = field(default_factory=dict)   # 客观差分
    duration_ms: float = 0.0
    observation: str = ""             # 一行人类可读的事实（由差分生成）


def _snapshot(world: Dict, who: str) -> Dict[str, Any]:
    """行动前后可比对的状态切片（只取会变的）。"""
    actor = (world.get("actors") or {}).get(who) or {}
    return {
        "position": actor.get("position"),
        "inventory": dict(actor.get("inventory", {})),
        "stamina": stamina_of(world, who),
        "delivered": dict(world.get("delivered", {})),
        "log_len": len(world.get("log", [])),
    }


def _diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """只记**变了**的键（资源类给增量，位置/耐力给 from→to）。"""
    out: Dict[str, Any] = {}
    for key in ("position", "stamina", "log_len"):
        if before.get(key) != after.get(key):
            out[key] = {"from": before.get(key), "to": after.get(key)}
    for key in ("inventory", "delivered"):
        b, a = before.get(key) or {}, after.get(key) or {}
        delta = {k: a.get(k, 0) - b.get(k, 0) for k in set(b) | set(a)}
        delta = {k: v for k, v in delta.items() if v}
        if delta:
            out[key] = delta
    return out


def classify_error(message: str) -> Optional[str]:
    """失败消息 → error_code；匹配不到 → None（不猜）。"""
    for pattern, code in _ERROR_CODES:
        if pattern.search(message):
            return code
    return None


def _observe(who: str, action: str, ok: bool, changes: Dict[str, Any]) -> str:
    """一行事实摘要（只描述差分里真实发生的事，不解释、不预测）。"""
    if not ok:
        return f"{who} 的 {action} 未生效"
    parts = []
    if "position" in changes:
        parts.append(f"位置 {changes['position']['from']}→{changes['position']['to']}")
    for res, d in (changes.get("inventory") or {}).items():
        parts.append(f"背包 {res}{d:+d}")
    for res, d in (changes.get("delivered") or {}).items():
        parts.append(f"累计交付 {res}{d:+d}")
    if "stamina" in changes:
        parts.append(f"耐力 {changes['stamina']['from']}→{changes['stamina']['to']}")
    return f"{who} {action}: " + ("，".join(parts) if parts else "已执行")


def apply_action_structured(world: Dict, action: str, params: Optional[Dict] = None,
                            who: str = "cang") -> Tuple[Dict, ActionResult]:
    """`apply_action` 的薄包装：照旧改世界，额外把后果整理成 `ActionResult`。

    语义与 `apply_action` **完全一致**（同一个函数、同一份世界对象、同样的成败判定）。
    """
    params = params or {}
    before = _snapshot(world, who)
    started = time.perf_counter()
    world, ok, message = apply_action(world, action, params, who=who)
    duration_ms = (time.perf_counter() - started) * 1000.0
    changes = _diff(before, _snapshot(world, who))
    result = ActionResult(
        action=action,
        who=who,
        success=bool(ok),
        message=message,
        error_code=None if ok else classify_error(message),
        world_changes=changes,
        duration_ms=round(duration_ms, 3),
        observation=_observe(who, action, bool(ok), changes),
    )
    return world, result
