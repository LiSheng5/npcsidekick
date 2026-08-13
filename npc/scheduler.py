"""
NPCSidekick — 调度（自主 tick 循环 + 主角互动优先）。

自主循环（AI Town 模式，v1 实现 — 文件头注释曾预留"v2 挂进循环"，现在就是它）:
  - 大脑驱动: 服务器后台每 TICK_INTERVAL 推一帧 tick_round()
  - 每 tick 每 NPC 恰一步（一步 = 走一跳 / 采一次 / 交付一次 / 一个休息 tick）
  - 多 NPC 交错推进，纯函数零 I/O — save() 由调用方按转换事件（started/completed/failed）触发
  - 零 LLM: 普通 NPC 轻路径（设计点 B 复杂度分级）— 决策全规则，LLM 只留在对话层

约束（调用方契约）: tick_round 必须单线程串行调用（原地修改共享 world）。
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional

from npc.world import apply_action, find_path

# 失败项冷却 tick 数（资源采尽后不让 NPC 反复撞同一堵墙）
BLOCK_AFTER_FAIL_TICKS = 5
# 休息时碰面说话的概率（说台词 = 只写 log，零代价 — 游戏侧差分显示气泡）
CHAT_CHANCE = 0.5
# 资源再生: 每 tick 各地点资源向初始值回补（零惩罚哲学 — 游戏侧树 60s 重生,大脑侧同构）
RESOURCE_REGEN_PER_TICK = 1
_RESOURCE_CAP_FALLBACK = 999   # 旧记忆卡无 _resource_caps 时用初始值兜底


def _others_here(world: Dict, who: str) -> bool:
    """同一地点有没有其他 actor（NPC 或主角）。"""
    pos = world["actors"][who]["position"]
    if world["protagonist"]["position"] == pos:
        return True
    return any(aid != who and a["position"] == pos for aid, a in world["actors"].items())


def _pick_rules_line(persona: Dict, rng: random.Random) -> str:
    """随机一句规则台词（replies 值；无则 fallback）。"""
    rules = persona.get("rules", {})
    replies = rules.get("replies", {})
    if replies:
        return rng.choice(list(replies.values()))
    return rules.get("fallback", "嗯。")


def resource_site(world: Dict, current: str, resource: str) -> Optional[str]:
    """有该资源且余量 > 0 的地点，按路径长度取最近可达；无 → None。"""
    candidates = []
    for name, loc in world["locations"].items():
        if resource in loc.get("resources", {}) and loc["resources"][resource] > 0:
            path = find_path(world, current, name)
            if path is not None:
                candidates.append((len(path), name))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _walk_steps(world: Dict, start: str, dest: str) -> Optional[List[Dict]]:
    """走向 dest 的 walk 步骤列表（每 tick 走一跳；到达后每步重算 BFS 自动改道）。

    walk 步骤只存 target — 主角移动/路径变化时按目标重新寻路，不会整链失效。
    """
    path = find_path(world, start, dest)
    if path is None:
        return None
    return [{"kind": "walk", "target": dest} for _ in path]


def _plan_steps(npc, world: Dict, item: Dict) -> Optional[List[Dict]]:
    """routine 项 → 步骤列表。gather 天然含"运回"（走→采×count→走到主角处→交付×count）。"""
    action = item["action"]
    if action == "rest":
        return [{"kind": "rest"} for _ in range(int(item.get("ticks", 2)))]
    if action == "say":
        return [{"kind": "say"}]

    resource = item["resource"]
    who = npc.actor_id
    start = world["actors"][who]["position"]
    site = resource_site(world, start, resource)
    if site is None:
        return None
    steps = _walk_steps(world, start, site)
    if steps is None:
        return None
    count = int(item.get("count", 1))
    steps += [{"kind": "gather", "resource": resource}] * count
    dest = world["protagonist"]["position"]   # 交付 = 主角当前所在（v1 文本世界主角常在村庄）
    steps += _walk_steps(world, site, dest) or []
    steps += [{"kind": "deliver", "resource": resource}] * count
    return steps


def _describe(item: Dict) -> str:
    """活动展示描述（/api/state 给游戏/前端看）。"""
    action = item["action"]
    if action == "gather":
        return f"采集{item['resource']}×{item.get('count', 1)}"
    if action == "rest":
        return f"休息{item.get('ticks', 2)}刻"
    return "说句话"


def _choose_routine_item(npc, world: Dict, rng: random.Random) -> Optional[Dict]:
    """加权随机选下一个日常（跳过冷却中的失败项）。无 routine / 全被冷却 → None。"""
    routine = npc.persona.get("routine", [])
    if not routine:
        return None
    now = world["_tick"]
    candidates, weights = [], []
    for item in routine:
        if not isinstance(item, dict):
            continue   # 运行时防线: 手改 JSON 出错 → 跳过该项，NPC 顶多不动
        if npc._blocked.get((item.get("action"), item.get("resource")), 0) > now:
            continue
        candidates.append(item)
        w = item.get("weight", 1)
        weights.append(w if isinstance(w, (int, float)) and w > 0 else 1)
    if not candidates:
        return None
    return rng.choices(candidates, weights=weights, k=1)[0]


def _execute_step(npc, world: Dict, step: Dict, rng: random.Random) -> bool:
    """执行一步。失败返回 False（调用方中止活动 + 冷却 + 记忆）。"""
    who = npc.actor_id
    kind = step["kind"]

    if kind == "walk":
        path = find_path(world, world["actors"][who]["position"], step["target"])
        if path is None:
            return False
        if not path:      # 已在目标地点
            return True
        world, ok, _ = apply_action(world, "move", {"dest": path[0]}, who=who)
        return ok

    if kind in ("gather", "deliver"):
        world, ok, _ = apply_action(world, kind, {"resource": step["resource"]}, who=who)
        return ok

    if kind == "rest":
        if rng.random() < CHAT_CHANCE and _others_here(world, who):
            apply_action(world, "say", {"text": _pick_rules_line(npc.persona, rng)}, who=who)
        return True

    if kind == "say":
        if _others_here(world, who):
            apply_action(world, "say", {"text": _pick_rules_line(npc.persona, rng)}, who=who)
        return True

    return False


def _tick_one(npc, world: Dict, rng: random.Random) -> Dict:
    """一个 NPC 的一个 tick: 无活动 → 选新日常并开始；有活动 → 推进一步。返回转换事件。"""
    ev: Dict = {}

    if npc.activity is None:
        # 对话下的指令（"给我两根木材"）优先于自主日常 — 玩家 > 日常
        if npc.pending_task is not None:
            item = npc.pending_task
            npc.pending_task = None   # 接单后只执行一次（失败会进冷却）
        else:
            item = _choose_routine_item(npc, world, rng)
        if item is None:
            npc.state = "idle"
            return ev
        steps = _plan_steps(npc, world, item)
        if steps is None:
            npc.remember(f"想{item.get('action')}{item.get('resource', '')}但没找到地方", importance=4)
            npc.state = "idle"
            return ev
        desc = _describe(item)
        if not steps:   # 防御: 空步骤（如 count 非法归 0）— 直接视为完成
            npc.remember(f"完成：{desc}", importance=5)
            return ev
        npc.activity = {"item": item, "steps": steps, "desc": desc}
        npc.state = "walking"
        ev["started"] = desc

    step = npc.activity["steps"].pop(0)
    if not _execute_step(npc, world, step, rng):
        item = npc.activity["item"]
        npc._blocked[(item.get("action"), item.get("resource"))] = world["_tick"] + BLOCK_AFTER_FAIL_TICKS
        desc = npc.activity["desc"]
        npc.remember(f"{desc}没做成", importance=4)
        npc.state = "idle"
        npc.activity = None
        ev["failed"] = desc
        return ev

    npc.state = {"walk": "walking", "gather": "working", "deliver": "working",
                 "rest": "resting", "say": "idle"}[step["kind"]]
    if not npc.activity["steps"]:
        desc = npc.activity["desc"]
        npc.remember(f"完成：{desc}", importance=5)
        npc.state = "idle"
        npc.activity = None
        ev["completed"] = desc
    return ev


def _regen_resources(world: Dict) -> None:
    """资源再生: 各地点资源向初始值回补（树会再长，浆果会再结 — 不会采空）。"""
    caps = world.get("_resource_caps", {})
    for loc in world["locations"].values():
        for res, count in loc.get("resources", {}).items():
            cap = caps.get(res, _RESOURCE_CAP_FALLBACK)
            if count < cap:
                loc["resources"][res] = min(cap, count + RESOURCE_REGEN_PER_TICK)


def tick_round(world: Dict, npcs: Dict, rng: Optional[random.Random] = None) -> Dict[str, Dict]:
    """推一帧自主循环: 每 NPC 恰一步，按 interaction_priority 顺序交错推进。

    返回 {actor_id: {"started"/"completed"/"failed": desc}} — 只有转换点的 NPC 出现。
    调用方（server 循环）按 events 决定 save — 本函数零 I/O。
    """
    rng = rng or random.Random()
    world["_tick"] = world.get("_tick", 0) + 1
    _regen_resources(world)
    events: Dict[str, Dict] = {}
    for actor_id in interaction_priority(world):
        ev = _tick_one(npcs[actor_id], world, rng)
        if ev:
            events[actor_id] = ev
    return events


def interaction_priority(
    world: Dict,
    recent_interactions: Optional[List[str]] = None,
) -> List[str]:
    """与主角互动的优先: 返回按优先级降序的 NPC id 列表。

    规则:
      1. 最近跟主角互动过的 NPC 置顶（保持互动连续性）
      2. 其余按到主角的路径长度升序（近者先 — 主角身边的先行动）
      3. 路径长度相同 → 维持注册顺序（稳定）
    """
    prot_pos = world["protagonist"]["position"]
    recent = list(recent_interactions or [])

    def key(npc_id: str) -> tuple[int, int]:
        # 第一个元素: 0=最近互动过(置顶), 1=其他; 第二个元素: 到主角的路径长度
        if npc_id in recent:
            return (0, 0)
        path = find_path(world, world["actors"][npc_id]["position"], prot_pos)
        distance = len(path) if path is not None else 9999
        return (1, distance)

    return sorted(world["actors"].keys(), key=key)
