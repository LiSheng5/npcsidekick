"""目标真值层（G2 · 2026-09-16）。

背景（《NPC大脑架构》§29.1-② / §29.3 G2）：
`persona.goals` 现状是**装饰** —— `{文本: {progress, target}}` 只被 `talk_pipeline`
拼进 prompt（`【目标】…`），**全仓没有任何一处写回 progress**，也不参与自主抽签。
本模块给它一套**真值**：目标有生命周期、有优先级、有前置条件、有截止帧，
进度由**代码**根据行动后果（G3 `ActionResult`）推进。

## 铁律（与项目"LLM 只提议、代码决定执行"同源）

- **状态只能由代码改**：`advance / apply_result / complete / fail / abandon / refresh`
  都是代码侧 API；LLM 想立目标或改目标，只能走既有的落账口（`NPC.book` → `guarded_book`）
  再由代码转成本模块的调用。**本模块不提供任何"接受模型输出"的入口。**
- **零 LLM / 零 I/O / 纯数据**：不读盘、不发请求、不碰 world（G1 接线由 scheduler 负责）。
- **向后兼容**：人设里的 `goals` 只是**初始种子**（`seed_from_persona`），
  坏数据跳过不炸（制作者友好）；没有 goals 的人设得到空队列。

用法：
    q = GoalQueue.seed_from_persona(npc.persona, world_tick=world["_tick"])
    world, result = apply_action_structured(world, "gather", {"resource": "木材"}, who="cang")
    q.apply_result("雪来之前再搭两个棚子", result, world_tick=world["_tick"])
    for g in q.active():            # G1 会拿这个列表去影响抽签
        ...
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

# ── 生命周期（6 态）────────────────────────────────────────────
PENDING = "pending"        # 没开始（前置未满足或刚建立）
ACTIVE = "active"          # 可推进
BLOCKED = "blocked"        # 前置未完成（refresh 算出来的）
COMPLETED = "completed"    # progress >= target
FAILED = "failed"          # 截止过了 / 判定做不成
ABANDONED = "abandoned"    # 主动放弃（人改主意）

ALL_STATUSES = (PENDING, ACTIVE, BLOCKED, COMPLETED, FAILED, ABANDONED)
TERMINAL_STATUSES = (COMPLETED, FAILED, ABANDONED)

DEFAULT_PRIORITY = 5       # 1..9，越大人越当回事（G1 拿它当权重先验）


def goal_id(text: str) -> str:
    """稳定 id：同一段目标文本永远得到同一个 id（便于持久化与引用）。"""
    digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:10]
    return f"g_{digest}"


@dataclass
class Goal:
    """一个目标（真值）。字段全可序列化，便于将来进记忆卡。"""

    text: str
    id: str = ""
    status: str = PENDING
    priority: int = DEFAULT_PRIORITY
    target: int = 1                       # 要多少次"推进"算完成
    progress: int = 0
    source: str = "code"                  # persona_seed / player / reflection / code
    created_tick: int = 0
    deadline_tick: Optional[int] = None   # 到帧未完成 → FAILED
    prerequisites: List[str] = field(default_factory=list)   # 其他 goal 的 id
    action: Optional[str] = None          # 可选：绑定的行动（G1 用它做候选）
    params: Dict[str, Any] = field(default_factory=dict)     # 行动参数（如 resource）

    def __post_init__(self) -> None:
        if not self.id:
            self.id = goal_id(self.text)
        if self.target < 1:
            self.target = 1
        self.priority = max(1, min(9, int(self.priority)))
        self.progress = max(0, int(self.progress))
        if self.status not in ALL_STATUSES:
            self.status = PENDING

    # ── 只读派生 ──
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def done(self) -> bool:
        return self.progress >= self.target

    @property
    def key(self) -> tuple:
        """G1 用的匹配键：(action, resource) —— 与 routine 项的冷却键同构。"""
        return (self.action, self.params.get("resource"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text, "id": self.id, "status": self.status,
            "priority": self.priority, "target": self.target, "progress": self.progress,
            "source": self.source, "created_tick": self.created_tick,
            "deadline_tick": self.deadline_tick, "prerequisites": list(self.prerequisites),
            "action": self.action, "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class GoalQueue:
    """目标队列 —— 本模块唯一的可变状态容器。**只有代码能改它。**"""

    def __init__(self, goals: Optional[List[Goal]] = None) -> None:
        self._goals: Dict[str, Goal] = {}
        for g in goals or []:
            self._goals[g.id] = g

    # ── 读 ──
    def __len__(self) -> int:
        return len(self._goals)

    def all(self) -> List[Goal]:
        return list(self._goals.values())

    def get(self, gid: str) -> Optional[Goal]:
        return self._goals.get(gid)

    def by_status(self, *statuses: str) -> List[Goal]:
        return [g for g in self._goals.values() if g.status in statuses]

    def active(self) -> List[Goal]:
        """可推进的目标（G1 的输入）。按优先级降序，同级按建立帧。"""
        return sorted(self.by_status(ACTIVE), key=lambda g: (-g.priority, g.created_tick))

    def actions_for(self, key: tuple) -> List[Goal]:
        """绑定了某个 (action, resource) 的活动目标（G1 用它给 routine 项加权）。"""
        return [g for g in self.active() if g.key == key]

    # ── 写（只有代码调这些）──
    def add(self, goal: Goal) -> Goal:
        """加入/替换（同 id 覆盖 —— 幂等，重复 seed 不会翻倍）。"""
        if goal.status == PENDING and not goal.prerequisites:
            goal = replace(goal, status=ACTIVE)
        self._goals[goal.id] = goal
        return goal

    def advance(self, gid: str, n: int = 1, world_tick: int = 0) -> Optional[Goal]:
        """推进进度；到达 target → COMPLETED。**已终结的目标不动**（不复活）。"""
        g = self._goals.get(gid)
        if g is None or g.is_terminal or n <= 0:
            return g
        g.progress = g.progress + int(n)
        if g.done:
            g.status = COMPLETED
        return g

    def apply_result(self, gid: str, result: Any, world_tick: int = 0) -> Optional[Goal]:
        """用 G3 的 `ActionResult` 推进：**成功才 +1**，失败不推进也不判死。

        这是 G3 的第一个消费点（`result.success` / `result.action`）。
        """
        if not getattr(result, "success", False):
            return self._goals.get(gid)
        return self.advance(gid, 1, world_tick=world_tick)

    def complete(self, gid: str) -> Optional[Goal]:
        g = self._goals.get(gid)
        if g is not None and not g.is_terminal:
            g.progress = max(g.progress, g.target)
            g.status = COMPLETED
        return g

    def fail(self, gid: str, reason: str = "") -> Optional[Goal]:
        g = self._goals.get(gid)
        if g is not None and not g.is_terminal:
            g.status = FAILED
        return g

    def abandon(self, gid: str) -> Optional[Goal]:
        g = self._goals.get(gid)
        if g is not None and not g.is_terminal:
            g.status = ABANDONED
        return g

    def refresh(self, world_tick: int = 0) -> Dict[str, List[str]]:
        """按世界帧刷新状态：算前置（BLOCKED/PENDING→ACTIVE）+ 判超期（→FAILED）。

        返回 {"unblocked": [id...], "expired": [id...]}（便于调用方记日志）。
        纯函数式语义：只看 prerequisites / deadline / 自身状态，不改 progress。
        """
        unblocked: List[str] = []
        expired: List[str] = []
        for g in list(self._goals.values()):
            if g.is_terminal:
                continue
            if g.deadline_tick is not None and world_tick > g.deadline_tick:
                g.status = FAILED
                expired.append(g.id)
                continue
            if g.prerequisites:
                blockers = [self._goals.get(p) for p in g.prerequisites]
                all_done = all(b is not None and b.status == COMPLETED for b in blockers)
                if not all_done:
                    g.status = BLOCKED
                    continue
            if g.status in (PENDING, BLOCKED):
                g.status = ACTIVE
                unblocked.append(g.id)
        return {"unblocked": unblocked, "expired": expired}

    # ── 种子 & 持久化 ──
    @classmethod
    def seed_from_persona(cls, persona: Optional[Dict], world_tick: int = 0) -> "GoalQueue":
        """人设 `goals` → 初始队列（**降为种子**：之后进度只由代码推进）。

        兼容现状形态 `{"目标文本": {"progress": 0, "target": 2}}`；
        坏数据（非 dict / target 非正数 / 文本空）**跳过不炸** —— 与 persona_loader 同款制作者友好。
        """
        q = cls()
        raw = (persona or {}).get("goals")
        if not isinstance(raw, dict):
            return q
        for text, spec in raw.items():
            if not isinstance(text, str) or not text.strip():
                continue
            spec = spec if isinstance(spec, dict) else {}
            try:
                target = int(spec.get("target", 1))
            except (TypeError, ValueError):
                target = 1
            try:
                progress = int(spec.get("progress", 0))
            except (TypeError, ValueError):
                progress = 0
            goal = Goal(
                text=text.strip(), target=target, progress=progress,
                source="persona_seed", created_tick=world_tick,
                status=COMPLETED if progress >= target else PENDING,
            )
            q.add(goal)
        return q

    def to_list(self) -> List[Dict[str, Any]]:
        return [g.to_dict() for g in self._goals.values()]

    @classmethod
    def from_list(cls, data: Optional[List[Dict[str, Any]]]) -> "GoalQueue":
        q = cls()
        for item in data or []:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                g = Goal.from_dict(item)
            except (TypeError, ValueError):
                continue
            q._goals[g.id] = g
        return q
