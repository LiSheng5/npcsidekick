"""待定动作登记 —— 结果还没定的动作按 `call_id` 存一张关联表。

同步做完的动作（`status: "done"` / 失败）当场写卡，**不经过这里**；本模块只管"还没定论"的：
  · mod 回 `status: "running"`（协议 §3）→ 登记，等 §4 的 `/api/action_result` 回报
  · 同步等待超时（不知道 mod 到底做没做）→ 登记，等回报；TTL 到点还没回报就当没做成

关联规则（协议 §4）：
  · 回报带 `call_id` → 精确取出（同一 NPC 同名动作并发也不会串）
  · 旧 mod 不带 `call_id` → 按 `(npc_id, action)` 取**最早**一条（FIFO），旧 mod 的回报不能丢

存活上限用 `NPC_PENDING_TTL`（秒，默认 300，现读可热切）。纯内存、无锁：
只在事件循环里同步改，方法内不留 `await`（同一实例挂在 `app.state.pending`）。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from uuid import uuid4

from core.config_flags import env_num
from core.logging_config import log

# 待定动作存活上限（秒）：超过 = 认定没做成，写一条失败记忆（清理是顺手做，不开后台任务）
DEFAULT_PENDING_TTL = 300.0
_ENV_PENDING_TTL = "NPC_PENDING_TTL"


def pending_ttl() -> float:
    """待定动作存活上限（现读，可热切）：NPC_PENDING_TTL 秒，默认 300。"""
    return max(0.0, env_num(_ENV_PENDING_TTL, DEFAULT_PENDING_TTL))


def new_call_id() -> str:
    """大脑生成的动作关联 id（`uuid4().hex`）。

    **不复用** LLM 的 tool_call id —— 那个由模型侧生成，不保证唯一（跨轮重复、缺失都可能）。
    """
    return uuid4().hex


class PendingActions:
    """`call_id -> {call_id, npc_id, action, params, mod, created_at}`，插入序即 FIFO 序。"""

    def __init__(self, ttl: Optional[float] = None) -> None:
        self._items: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._ttl = ttl        # None = 每次现读环境变量（可热切）

    def ttl(self) -> float:
        """本实例的 TTL（秒）：构造时给了就用给的，否则现读环境变量。"""
        return pending_ttl() if self._ttl is None else self._ttl

    def __len__(self) -> int:
        return len(self._items)

    def register(self, call_id: str, npc_id: str, action: str,
                 params: Optional[Dict[str, Any]] = None,
                 mod: Optional[str] = None) -> Dict[str, Any]:
        """登记一条待定动作（`running` / 超时未知）。同 id 重复登记 = 覆盖。"""
        info: Dict[str, Any] = {
            "call_id": call_id, "npc_id": npc_id, "action": action,
            "params": dict(params) if isinstance(params, dict) else {},
            "mod": mod, "created_at": time.time(),
        }
        self._items[call_id] = info
        log.info("pending_registered", call_id=call_id, npc_id=npc_id, action=action,
                 pending=len(self._items))
        return info

    def resolve(self, call_id: str) -> Optional[Dict[str, Any]]:
        """按 `call_id` 精确取出并移除（取不到 = 重复回报 / 已过期 / 未知）。"""
        return self._items.pop(call_id, None)

    def resolve_legacy(self, npc_id: str, action: str) -> Optional[Dict[str, Any]]:
        """旧 mod（回报不带 `call_id`）：按 `(npc_id, action)` 取最早一条并移除（FIFO）。"""
        for call_id, info in self._items.items():
            if info["npc_id"] == npc_id and info["action"] == action:
                del self._items[call_id]
                return info
        return None

    def expire(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """取出并移除超过 TTL 的条目（按登记序返回）；写失败记忆由调用方负责。"""
        moment = time.time() if now is None else now
        ttl = self.ttl()
        expired = [info for info in self._items.values()
                   if moment - float(info.get("created_at", moment)) > ttl]
        for info in expired:
            self._items.pop(info["call_id"], None)
        return expired
