"""协议 v1 任务回路（M1）— 消费者注册 + 任务账本 + 链式状态机 + 失败商议队列。

铁律不变: LLM 只提议、代码决定执行。账本即承诺（booked 才算数）。

设计要点（docs/协议v1_方案稿.md §四, 用户拍板口径）:
- 消费者表: mod 启动 POST /api/consumer/hello 报动词表; 之后每 ≤30s 重发 hello 即心跳,
  60s 无心跳判死（NPC_CONSUMER_TTL 可调）
- 白名单: B2 可编动词 = manifest.actions ∩ ∪活跃消费者.verbs;
  空集 → action_allowed()==False → 对话层不接活不承诺（事前防空承诺门）
- 账本: booked → dispatched → completed | failed | cancelled(detail="superseded"/"chain_failed")
- 链式: 同 chain_id 按 book 顺序派发, 前节 completed 才派下一节;
  任一节失败 → 该链剩余节点全部 cancelled(chain_failed)（账目清晰口径）
- 失败商议（用户拍板: 干砸了找玩家商量）: failed 时生成商议条目 ——
  服务层负责 ①写记忆卡("没做成: …") ②world log 追加 say 字幕推给 mod 显示;
  玩家之后问起时 _try_recall 能从记忆卡逐字召回（不编造）
- 僵尸账: dispatched 超 NPC_TASK_TIMEOUT(默认300s) 未销账 → failed(timeout) 进商议
- 持续型动词(follow_player/wander): dispatched 即视为在岗; 同 NPC 新落账时
  旧的非链上活动任务自动 cancelled(superseded)
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

_TERMINAL_STATES = ("completed", "failed", "cancelled")

# 持续型动词集合（GTA 方言; 其他方言后续经 actions.json 声明, v1 硬编码此二者）
CONTINUOUS_VERBS = frozenset({"follow_player", "wander", "fight"})


def consumer_ttl_sec() -> float:
    try:
        return float(os.environ.get("NPC_CONSUMER_TTL", "60"))
    except ValueError:
        return 60.0


def task_timeout_sec() -> float:
    try:
        return float(os.environ.get("NPC_TASK_TIMEOUT", "300"))
    except ValueError:
        return 300.0


def gate_enabled() -> bool:
    """协议门总开关 NPC_TASK_LOOP（方案稿审批项⑥修订版, 默认关）。

    关(默认): book() 行为与 v3.2 完全一致 —— 旧石器等内置执行器世界零回归;
    开(GTA bat): book() 过能力协商门 + 镜像进账本。端点本身恒在(空表惰性无副作用)。
    """
    return os.environ.get("NPC_TASK_LOOP", "") not in ("", "0")


class ConsumerRegistry:
    """消费者(游戏 mod)注册表: hello 报到 + 心跳过期 + 有效动词交集。"""

    def __init__(self) -> None:
        self._consumers: Dict[str, Dict] = {}

    def hello(self, name: str, version: str, verbs: Optional[List[str]]) -> Dict:
        key = str(name or "").strip()
        if not key:
            return {"ok": False, "error": "name 不能为空"}
        clean = sorted({str(v).strip() for v in (verbs or [])
                        if isinstance(v, str) and v.strip()})
        self._consumers[key] = {
            "version": str(version or ""),
            "verbs": frozenset(clean),
            "last_seen": time.monotonic(),
        }
        return {"ok": True, "name": key, "verbs": clean}

    def touch(self, name: str) -> None:
        c = self._consumers.get(str(name or ""))
        if c:
            c["last_seen"] = time.monotonic()

    def alive(self) -> Dict[str, Dict]:
        now = time.monotonic()
        ttl = consumer_ttl_sec()
        return {n: c for n, c in self._consumers.items()
                if now - c["last_seen"] <= ttl}

    def effective_verbs(self, manifest_actions) -> frozenset:
        """B2 可编动词 = manifest ∩ ∪活跃消费者.verbs; 无活跃消费者 → 空集。"""
        alive = self.alive()
        if not alive:
            return frozenset()
        union: set = set()
        for c in alive.values():
            union |= set(c["verbs"])
        return frozenset(union & set(manifest_actions or ()))

    def snapshot(self) -> Dict:
        alive = self.alive()
        return {"alive": len(alive),
                "consumers": {n: {"version": c["version"], "verbs": sorted(c["verbs"])}
                              for n, c in alive.items()}}


class TaskLedger:
    """任务账本: booked→dispatched→终态; 链式顺序派发; 失败商议队列。"""

    def __init__(self) -> None:
        self._seq = 0
        self._tasks: Dict[str, Dict] = {}
        self._discussions: Dict[str, List[Dict]] = {}   # npc_id → 待商议列表

    # ── 落账 ──
    def book(self, npc_id: str, action: str, params: Optional[Dict] = None,
             chain_id: Optional[str] = None, desc: str = "") -> Dict:
        self._seq += 1
        t = {
            "task_id": f"t_{self._seq}",
            "npc_id": str(npc_id),
            "action": str(action),
            "params": dict(params or {}),
            "chain_id": chain_id,
            "desc": desc or action,
            "state": "booked",
            "error": "",
            "created_at": time.time(),
            "dispatched_at": 0.0,
            "done_at": 0.0,
        }
        self._tasks[t["task_id"]] = t
        # 持续型/独立任务: 新落账 supersede 同 NPC 的旧在岗任务（同链除外）
        for other in self._tasks.values():
            if (other["npc_id"] == t["npc_id"]
                    and other["state"] in ("booked", "dispatched")
                    and other["task_id"] != t["task_id"]
                    and other["chain_id"] != (chain_id or f"@{t['task_id']}")):
                if not (chain_id and other["chain_id"] == chain_id):
                    other["state"] = "cancelled"
                    other["error"] = "superseded"
                    other["done_at"] = time.time()
        return dict(t)

    # ── 派发视图（/api/state 挂载）──
    def dispatch_view(self) -> List[Dict]:
        """booked → dispatched（首次下发即标记），返回当前应让 mod 看到的活动任务。"""
        out: List[Dict] = []
        for t in sorted(self._tasks.values(), key=lambda x: x["created_at"]):
            if t["state"] == "dispatched":
                out.append(dict(t))
            elif t["state"] == "booked" and self._chain_ready(t):
                t["state"] = "dispatched"
                t["dispatched_at"] = time.time()
                out.append(dict(t))
        return out

    def _chain_ready(self, t: Dict) -> bool:
        """链式闸: 同链且先于本节落账的节点全部 completed, 本节才可派发。"""
        if not t["chain_id"]:
            return True
        for other in self._tasks.values():
            if (other["chain_id"] == t["chain_id"]
                    and other["created_at"] < t["created_at"]
                    and other["state"] != "completed"):
                return False
        return True

    # ── 销账 ──
    def settle(self, task_id: str, status: str, detail: str = "") -> Optional[Dict]:
        """mod 回报销账。completed/failed 合法; failed 触发链式取消+商议入队。"""
        t = self._tasks.get(str(task_id))
        if t is None or t["state"] in _TERMINAL_STATES:
            return None
        now = time.time()
        if status == "completed":
            t["state"] = "completed"
            t["done_at"] = now
            return dict(t)
        if status == "failed":
            self.fail(str(task_id), detail or "unknown")
            return dict(t)
        if status == "cancelled":
            t["state"] = "cancelled"
            t["error"] = detail or "by_consumer"
            t["done_at"] = now
            return dict(t)
        return None

    def fail(self, task_id: str, error: str) -> Optional[Dict]:
        """失败: 本节 failed → 同链剩余未完成节点整链取消 → 商议入队（用户拍板口径）。"""
        t = self._tasks.get(str(task_id))
        if t is None or t["state"] in _TERMINAL_STATES:
            return None
        t["state"] = "failed"
        t["error"] = str(error or "unknown")
        t["done_at"] = time.time()
        if t["chain_id"]:
            for other in self._tasks.values():
                if (other["chain_id"] == t["chain_id"]
                        and other["state"] in ("booked", "dispatched")):
                    other["state"] = "cancelled"
                    other["error"] = "chain_failed"
                    other["done_at"] = time.time()
        self._discussions.setdefault(t["npc_id"], []).append({
            "task_id": t["task_id"],
            "action": t["action"],
            "params": dict(t["params"]),
            "error": t["error"],
            "text": f"「{t['desc']}」没办成（{t['error']}）。咱们商量下？",
            "at": time.time(),
        })
        return dict(t)

    def reap_zombies(self) -> List[Dict]:
        """dispatched 超 NPC_TASK_TIMEOUT 未销账 → failed(timeout) 进商议。"""
        now = time.time()
        limit = task_timeout_sec()
        reaped = []
        for t in self._tasks.values():
            if (t["state"] == "dispatched"
                    and t["dispatched_at"] > 0
                    and now - t["dispatched_at"] > limit):
                self.fail(t["task_id"], "timeout")
                reaped.append(t["task_id"])
        return reaped

    # ── 查询 ──
    def discussions(self, npc_id: str) -> List[Dict]:
        return list(self._discussions.get(str(npc_id), []))

    def pop_discussions(self, npc_id: str) -> List[Dict]:
        """玩家来对话时取走商议条目（NPC 先主动提起）。"""
        return self._discussions.pop(str(npc_id), [])

    def stats(self) -> Dict:
        by_state: Dict[str, int] = {}
        for t in self._tasks.values():
            by_state[t["state"]] = by_state.get(t["state"], 0) + 1
        return {"total": len(self._tasks), "by_state": by_state,
                "pending_discussions": sum(len(v) for v in self._discussions.values())}


def action_allowed(action: str, manifest_actions) -> bool:
    """事前防空承诺门: 动词 ∈ manifest 且 ∈ 活跃消费者并集。"""
    return str(action) in REGISTRY.effective_verbs(manifest_actions)


REGISTRY = ConsumerRegistry()
LEDGER = TaskLedger()
