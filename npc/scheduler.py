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

# ── 耐力系统(2026-08-22) ——————————————————————————————
from npc.world import STAMINA_MAX, stamina_of   # 动作扣减在 world.apply_action 统一收口

STAMINA_REST_RECOVER = 30     # [PLACEHOLDER] 每 rest tick 恢复(3s 一跳:3 刻 ≈ 半池)
STAMINA_TIRED = 60.0          # [PLACEHOLDER] 低于此 = 疲惫(干活降权,休息升权)
STAMINA_EXHAUSTED = 30.0      # [PLACEHOLDER] 低于此 = 力竭(干活重降权,休息重升权)

# ── 游戏内昼夜(与游戏端 WorldTime 同构:3 分钟 = 1 游戏小时;tick 3s → 60 tick/小时) ——
TICKS_PER_GAME_HOUR = 60
DAY_START_HOUR = 8            # 服务器启动 = 早晨 8 点


from agent.config_flags import env_flag
from npc.memory import EV_DONE, EV_FAIL, goal_relevance_enabled


def _protocol_owns_pending() -> bool:
    """协议 v1(M2 划界·2026-08-25): NPC_TASK_LOOP 开启时 pending_task 归外部
    消费者(mod 经 /api/state 认领、/api/task_done 销账), 文本执行引擎不抢跑 ——
    防双消费者竞态(账本把已完成的活误判僵尸/双份执行)。惰性导入防环。"""
    try:
        from npc import taskloop as _taskloop
        return _taskloop.gate_enabled()
    except Exception:
        return False


def game_hour(world: Dict) -> int:
    """当前游戏小时(0~23)。游戏端经 /api/talk context 同步的真实时间优先
    (天气/昼夜权重用真实数据,不再猜);无同步时按 tick 自推(60 tick=1小时,同构近似)。"""
    gh = world.get("_game_hour")
    if gh is not None:
        return int(gh)
    return (DAY_START_HOUR + world.get("_tick", 0) // TICKS_PER_GAME_HOUR) % 24


def _is_night(hour: int) -> bool:
    return hour >= 22 or hour < 6   # [PLACEHOLDER] 夜间 22:00~06:00


# ── 目标驱动（G1 · 2026-09-16 · 开关 NPC_GOALS，默认关）────────────────────
# 关 = 与旧版逐字节一致：目标层**完全不参与**决策（连种子都不建、连 npc.goal 都不导入）。
# 开 = 活动目标两件事：(a) 给同 (action, resource) 的日常项**抬权**；
#      (b) 日常表里没有的活**补成候选**（目标能创造新工作）。
GOAL_WEIGHT_BASE = 3.0     # [PLACEHOLDER] 优先级 9 → ×3 / 5 → ×1（中性）/ 1 → ×1/3
GOAL_EXTRA_WEIGHT = 2.0    # [PLACEHOLDER] 目标新增候选的基础权重（再叠 _dynamic_weight）


def goals_enabled() -> bool:
    """G1 开关（现读现切，家规）：NPC_GOALS=1 时目标层参与自主抽签。"""
    return env_flag("NPC_GOALS")


def _mech_try(fn, *args):
    """目标层是**增益**、不是闸门：任何异常都降级吞掉，绝不拖垮自主循环。"""
    try:
        return fn(*args)
    except Exception:
        return None


def npc_goals(npc, world: Dict):
    """拿这个 NPC 的目标队列：首次从人设 `goals` 播种，此后每次现读刷新状态。

    只在 `goals_enabled()` 时被调用 —— 关着的时候连 `npc.goal` 都不导入（零开销）。
    """
    from npc.goal import GoalQueue          # 惰性导入: 关着时不加载
    q = getattr(npc, "_goal_queue", None)
    if q is None:
        q = GoalQueue.seed_from_persona(npc.persona, world_tick=world.get("_tick", 0))
        npc._goal_queue = q
    q.refresh(world.get("_tick", 0))
    return q


def _item_key(item: Dict) -> tuple:
    """抽签项的匹配键（与失败冷却键同构）。"""
    return (item.get("action"), item.get("resource"))


def _goal_factor(npc, world: Dict, item: Dict) -> float:
    """活动目标对这件活的抬权系数（纯数值，零 LLM）。

    命中 `GoalQueue.actions_for((action, resource))` → 按**最高优先级**抬权：
    9 → ×GOAL_WEIGHT_BASE、5 → ×1（中性）、1 → ×1/GOAL_WEIGHT_BASE；没命中/出故障 → ×1。
    """
    def _calc() -> float:
        q = npc_goals(npc, world)
        hits = q.actions_for(_item_key(item))
        if not hits:
            return 1.0
        return float(GOAL_WEIGHT_BASE ** ((max(g.priority for g in hits) - 5) / 4.0))

    factor = _mech_try(_calc)
    return factor if isinstance(factor, float) else 1.0


def _goal_candidates(npc, world: Dict) -> List[Dict]:
    """活动目标 → 候选项（只取**绑定了 action** 的：没绑动作的目标只能靠玩家单，不自主开工）。"""
    out: List[Dict] = []
    q = _mech_try(npc_goals, npc, world)
    if q is None:
        return out
    for g in q.active():
        if not g.action:
            continue
        item: Dict = {"action": g.action, "weight": GOAL_EXTRA_WEIGHT, "_goal": g.id}
        item.update(g.params)
        item.setdefault("count", 1)
        out.append(item)
    return out


def _advance_goals_on_complete(npc, world: Dict, item: Dict) -> None:
    """一次活动**完整做完** → 命中该 (action, resource) 的活动目标各 +1 进度。

    G3 后果 → G2 目标的消费点（整链成功 = 这一件活成了）。
    """
    q = _mech_try(npc_goals, npc, world)
    if q is None:
        return
    for g in q.actions_for(_item_key(item)):
        q.advance(g.id, 1, world_tick=world.get("_tick", 0))


# ── 反思 lesson 进决策（G1 另一半 · 2026-09-16 · 开关 NPC_LESSONS，默认关）──────
# 只认"同时带 scope 与 recommendation"的反思条目（老条目 → 不参与决策）。
# recommendation ∈ [-1,1] → 权重乘子 = LESSON_WEIGHT_BASE ** rec：
#   +1 → ×2（经验说"该多做"）/ 0 → ×1（中性）/ -1 → ×0.5（说"该少做"）。
LESSON_WEIGHT_BASE = 2.0   # [PLACEHOLDER]


def lessons_enabled() -> bool:
    """G1 另一半的开关（现读现切，家规）：NPC_LESSONS=1 时反思 lesson 参与自主抽签。"""
    return env_flag("NPC_LESSONS")


def _lesson_factor(npc, world: Dict, item: Dict) -> float:
    """作用域命中的反思 lesson 对这件活的权重乘子（纯数值，零 LLM）。

    多命中时取**绝对值最大**的一条（单条最强说了算 —— 避免层层相乘把权重打爆）；
    没命中 / 记忆不可用 / 出故障 → ×1。
    """
    def _calc() -> float:
        mem = getattr(npc, "memory", None)
        if mem is None or not hasattr(mem, "lessons_for"):
            return 1.0
        hits = mem.lessons_for(item)
        if not hits:
            return 1.0
        return float(LESSON_WEIGHT_BASE ** float(hits[0]["recommendation"]))

    factor = _mech_try(_calc)
    return factor if isinstance(factor, float) else 1.0


def _dynamic_weight(npc, world: Dict, item: Dict, base: float) -> float:
    """静态 weight → 动态权重:生理(耐力)×昼夜×库存缺口。
    纯数值零 LLM;参数全 [PLACEHOLDER],后续按真实生理/行为学再调比例。
    """
    w = float(base)
    action = item.get("action", "")
    st = stamina_of(world, npc.actor_id)
    night = _is_night(game_hour(world))

    weather = world.get("_weather", "")   # 游戏同步的真实天气(未同步=晴)

    if action == "rest":
        if st < STAMINA_EXHAUSTED:
            w *= 8.0            # 力竭:休息压倒一切
        elif st < STAMINA_TIRED:
            w *= 3.0            # 疲惫:明显想歇
        if night:
            w *= 2.0            # 夜里犯困
        if weather == "rain":
            w *= 1.5            # [PLACEHOLDER] 雨天躲雨多歇
        elif weather == "festival":
            w *= 0.7            # 节庆别老躺着,出来热闹
    elif action in ("gather", "craft"):
        if st < STAMINA_EXHAUSTED:
            w *= 0.1            # 力竭:几乎不接重活(不硬禁 — 随机性保留"硬撑")
        elif st < STAMINA_TIRED:
            w *= 0.5            # 疲惫:干活意愿减半
        if night:
            w *= 0.4            # 夜里不进森林/不开工
        if weather == "rain":
            w *= 0.5            # [PLACEHOLDER] 雨天干活降权(淋湿又冷又没劲)
        elif weather == "festival":
            w *= 0.6            # 节庆少干活
        if action == "gather":
            # 库存缺口:主角已收该资源越少越优先(部落存量思维)
            res = item.get("resource", "")
            got = world.get("delivered", {}).get(res, 0)
            if got < 5:
                w *= 1.6       # [PLACEHOLDER] 缺口放大
            elif got > 20:
                w *= 0.5       # 满仓降权(别堆一座山)
    elif action == "deliver":
        pass                    # 交付走 pending_task 玩家单优先,日常不在此列

    if goals_enabled():
        # G1(2026-09-16): 目标驱动 —— 活动目标要这件活 → 按优先级抬权(纯数值,零 LLM)
        w *= _goal_factor(npc, world, item)
    if lessons_enabled():
        # G1 另一半(2026-09-16): 反思 lesson(scope 命中)调权 —— 「反思改变行为」的落点
        w *= _lesson_factor(npc, world, item)
    return w


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


# ── routine 计划（T-01·2026-09-15: 计划是"总函数" — 坏项返回 None，绝不抛异常）──
# 引擎能编排的动作 = 与 _execute_step 认识的 step.kind 对应（rest/say/gather）。
# 其余动作 loader 允许写（不替游戏做决定），能不能执行由 Runtime 说了算:
# 这里认不出来 → 计划失败（记一条记忆 + 进冷却），不崩。
SUPPORTED_ROUTINE_ACTIONS = ("gather", "rest", "say")


def _plan_failure_note(item: Dict) -> str:
    """计划失败的人类可读原因（写进记忆，Memory 页可见）。

    分支与原因一一对应，别把"引擎不会"说成"没找到地方"。
    """
    action = item.get("action")
    if action not in SUPPORTED_ROUTINE_ACTIONS:
        return f"想做{action}但引擎还没有这个动作"
    if not item.get("resource"):
        return f"想做{action}但没写清要什么"
    return f"想{action}{item.get('resource', '')}但没找到地方"


def _plan_steps(npc, world: Dict, item: Dict) -> Optional[List[Dict]]:
    """routine 项 → 步骤列表；引擎编排不了的项 → None（调用方记一条 + 冷却）。

    gather 天然含"运回"（走→采×count→走到主角处→交付×count）。

    旧版对非 rest/say 项一律走 gather 链（`item["resource"]`）→ craft / 自定义动作 /
    缺 resource 的 gather 全 KeyError;异常冒到 server._tick_loop 的 except 会吞掉
    整帧（落盘/反思/管家/账本回收一起跳过），NPC 每帧静默空转且零日志线索。
    """
    action = item.get("action")
    if action == "rest":
        return [{"kind": "rest"} for _ in range(int(item.get("ticks", 2)))]
    if action == "say":
        return [{"kind": "say"}]
    if action != "gather":
        return None                     # craft / 自定义动作: 引擎没有对应步骤
    resource = item.get("resource")
    if not isinstance(resource, str) or not resource:
        return None                     # gather 缺 resource（loader 明许）→ 无从下手

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
    """加权随机选下一个日常（跳过冷却中的失败项）。无候选 → None。

    G1(NPC_GOALS=1): 候选 = routine ∪ 活动目标绑定的活；routine 里已有的不重复造项（靠抬权）。
    """
    routine = npc.persona.get("routine") or []
    if not isinstance(routine, list):
        routine = []          # 手改 JSON 写成字符串/None → 当"没有日常表"（别拿它去迭代）
    goal_mode = goals_enabled()
    if not routine and not goal_mode:
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
        base = w if isinstance(w, (int, float)) and w > 0 else 1
        # 动态权重(2026-08-22): persona 静态 weight × 耐力 × 昼夜 × 库存缺口
        weights.append(_dynamic_weight(npc, world, item, base))
    if goal_mode:
        seen = {_item_key(i) for i in candidates}
        for gi in _goal_candidates(npc, world):
            if _item_key(gi) in seen:
                continue                  # 这个活 routine 里已有 → 只靠 _goal_factor 抬权
            if npc._blocked.get(_item_key(gi), 0) > now:
                continue                  # 目标也吃冷却：撞过墙的活别连着撞
            candidates.append(gi)
            weights.append(_dynamic_weight(npc, world, gi, float(gi["weight"])))
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
        actor = world["actors"][who]
        actor["stamina"] = min(STAMINA_MAX, stamina_of(world, who) + STAMINA_REST_RECOVER)
        if rng.random() < CHAT_CHANCE and _others_here(world, who):
            apply_action(world, "say", {"text": _pick_rules_line(npc.persona, rng)}, who=who)
        return True

    if kind == "say":
        if _others_here(world, who):
            apply_action(world, "say", {"text": _pick_rules_line(npc.persona, rng)}, who=who)
        return True

    return False


def _sync_goal_terms(npc, world: Dict) -> None:
    """把当前活动目标文本注入记忆层（P-6：检索时按 goal_relevance 加分）。

    只有 NPC_GOALS 或 NPC_GOAL_RELEVANCE 任一开启时才会被调用 —— 两个都关时
    连目标队列都不建（零开销、零行为变化）。
    """
    q = npc_goals(npc, world)
    mem = getattr(npc, "memory", None)
    if q is not None and mem is not None and hasattr(mem, "set_goal_terms"):
        mem.set_goal_terms([g.text for g in q.active()])


def _tick_one(npc, world: Dict, rng: random.Random) -> Dict:
    """一个 NPC 的一个 tick: 无活动 → 选新日常并开始；有活动 → 推进一步。返回转换事件。"""
    ev: Dict = {}

    if goals_enabled() or goal_relevance_enabled():
        _mech_try(_sync_goal_terms, npc, world)   # P-6: 目标 → 记忆检索打分的注入点

    if npc.activity is None:
        # 对话下的指令（"给我两根木材"）优先于自主日常 — 玩家 > 日常
        # 协议模式(NPC_TASK_LOOP=1)例外: 玩家单归外部消费者执行, 引擎不抢跑
        if npc.pending_task is not None and not _protocol_owns_pending():
            item = npc.pending_task
            npc.pending_task = None   # 接单后只执行一次（失败会进冷却）
        else:
            item = _choose_routine_item(npc, world, rng)
        if item is None:
            npc.state = "idle"
            return ev
        steps = _plan_steps(npc, world, item)
        if steps is None:
            # T-01(2026-09-15): 计划失败也要进冷却 —— 否则每帧重选同一项 → 每帧写一条
            # 记忆（记忆卡刷屏）。冷却键与执行失败同款 (action, resource)，重试窗口一致。
            npc._blocked[(item.get("action"), item.get("resource"))] = (
                world["_tick"] + BLOCK_AFTER_FAIL_TICKS)
            npc.remember(_plan_failure_note(item), importance=4)
            npc.state = "idle"
            return ev
        desc = _describe(item)
        if not steps:   # 防御: 空步骤（如 count 非法归 0）— 直接视为完成
            npc.remember(f"{EV_DONE}{desc}", importance=5)
            return ev
        npc.activity = {"item": item, "steps": steps, "desc": desc}
        npc.state = "walking"
        ev["started"] = desc

    step = npc.activity["steps"].pop(0)
    if not _execute_step(npc, world, step, rng):
        item = npc.activity["item"]
        npc._blocked[(item.get("action"), item.get("resource"))] = world["_tick"] + BLOCK_AFTER_FAIL_TICKS
        desc = npc.activity["desc"]
        npc.remember(f"{EV_FAIL}{desc}", importance=4)
        npc.state = "idle"
        npc.activity = None
        ev["failed"] = desc
        return ev

    npc.state = {"walk": "walking", "gather": "working", "deliver": "working",
                 "rest": "resting", "say": "idle"}[step["kind"]]
    if not npc.activity["steps"]:
        item = npc.activity["item"]
        desc = npc.activity["desc"]
        if goals_enabled():
            _mech_try(_advance_goals_on_complete, npc, world, item)   # G1: 干完 → 目标 +1
        npc.remember(f"{EV_DONE}{desc}", importance=5)
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
        if actor_id not in npcs:
            continue   # 动态注册竞态防线: actor 槽在而 NPC 已反注册 → 跳过不炸
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


# ── LLM 调度队列（任务书 #01，开关 NPC_SCHEDULER，默认 OFF）────────────────
# 目标：把 LLM 调用从事件循环里拆出去，由单线程 worker 串行执行，按优先级排队
# （对话 > 审查 > 编译 > 反思）；对话排队超时自动落回本地规则兜底。
# 铁律 1：NPC_SCHEDULER 未设 / 空 / "0" → 所有代码路径与现状完全一致（不经过任何新代码）。
import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor


# 优先级常量（数值越小越优先）
P_TALK = 0       # 对话 (B1)
P_REVIEW = 1     # A 审查 / review 槽调用
P_COMPILE = 2    # B2 任务编译
P_REFLECT = 3    # 反思总结

_PRIO_NAME = {P_TALK: "talk", P_REVIEW: "review", P_COMPILE: "compile", P_REFLECT: "reflect"}


class SchedulerTimeout(Exception):
    """排队等待超过 timeout 秒仍未执行 → 抛出（调用方落兜底逻辑）。"""


def _scheduler_enabled() -> bool:
    """开关读取：不设 / 空 / "0" = OFF；其余（如 "1"）= ON。每次现读，可热切。"""
    return env_flag("NPC_SCHEDULER")


class LLMScheduler:
    """LLM 调度器：asyncio.PriorityQueue + 专职 worker task + 单线程 executor。

    - worker task 在事件循环线程消费队列；执行体交给 ThreadPoolExecutor(max_workers=1)，
      保住"同一时刻全进程只有一个 LLM 请求在飞"的硬约束（上游网关免费通道串行）。
    - 嵌套直通（防死锁，最重要）：worker 线程内的 fn 再调 run()/invoke() 时不入队、
      直接执行 —— 端点层把整个 npc.talk 以 P_TALK 入队，talk 内部还会触发审查/B2 的
      LLM 调用，它们若再排队就是自己等自己。
    """

    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
        # PriorityQueue 元素 = (priority, seq, (future, fn, args, kwargs))；seq 做次键 FIFO
        # 注：队列延迟到 start() 按当前 running loop 创建（asyncio 队列会绑定首次使用的 loop，
        # 跨生命周期复用会绑定到已关闭的 loop 导致 hang；pytest-asyncio 每测试一个 loop）。
        self._queue: asyncio.PriorityQueue = None
        self._worker_task = None
        self._seq = 0
        self._tlocal = threading.local()
        # 串行闸：sync invoke 与 executor 里的对话 LLM 互斥（进程级单 LLM 在飞）
        self._serial_lock = threading.Lock()
        # 指标（进程内存态，重启清零；/api/stats 读 snapshot()）
        self._depth = {"talk": 0, "review": 0, "compile": 0, "reflect": 0}
        self._waits = 0
        self._timeouts = 0
        self._wait_ms_total = 0

    # ── 开关 / 状态 ──
    @property
    def enabled(self) -> bool:
        return _scheduler_enabled()

    @property
    def started(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    def _in_worker(self) -> bool:
        return bool(getattr(self._tlocal, "in_worker", False))

    # ── 生命周期（FastAPI lifespan 挂载 / 卸载）──
    def start(self) -> None:
        if self.started:
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1)
        # 每次 start 重建队列：绑定当前 running loop，避免跨 loop 复用导致 hang
        self._queue = asyncio.PriorityQueue()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None
        # 清空队列里未执行任务，取消其 future（否则 run() 的 await 永远挂起）
        try:
            while True:
                _p, _s, item = self._queue.get_nowait()
                fut = item[0]
                if not fut.done():
                    fut.cancel()
        except asyncio.QueueEmpty:
            pass
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    # ── 核心：异步优先级入队（server talk 端点用）──
    async def run(self, priority: int, fn, /, *args, timeout: Optional[float] = None, **kwargs):
        if self._in_worker():
            return fn(*args, **kwargs)   # 嵌套直通：已在 worker 内，直接执行防死锁
        if not self.enabled:
            return fn(*args, **kwargs)   # OFF：零变化（与旧行为逐字节一致）
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._seq += 1
        seq = self._seq
        name = _PRIO_NAME.get(priority, "reflect")
        self._depth[name] += 1
        self._waits += 1
        t0 = time.monotonic()
        await self._queue.put((priority, seq, (fut, fn, args, kwargs)))
        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            fut.cancel()                 # 标记 skip：worker 看到 cancelled 就跳过不执行
            self._timeouts += 1
            raise SchedulerTimeout(f"调度等待超过 {timeout}s，未执行") from None
        finally:
            self._depth[name] -= 1
            self._wait_ms_total += int((time.monotonic() - t0) * 1000)

    def invoke(self, priority: int, fn, /, *args, timeout: Optional[float] = None, **kwargs):
        """同步入口（npc.py / subagent.py 内部 LLM 调用点）。

        - worker 线程内 → 嵌套直通，直接执行（防自我死锁：worker 已持锁）
        - OFF → 直接执行（零变化）
        - ON 且非 worker → 持 _serial_lock 执行，与 executor 里的对话 LLM 互斥：
          保证「同一时刻全进程只有一个 LLM 请求在飞」。反思从 _tick_loop（loop 线程）
          发起的 llm.chat 与正在跑的对话 LLM 由此不再并发双飞。
        """
        if self._in_worker():
            return fn(*args, **kwargs)
        if not self.enabled:
            return fn(*args, **kwargs)
        with self._serial_lock:
            return fn(*args, **kwargs)

    # ── worker ──
    async def _worker_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            if item is None:
                continue
            _priority, _seq, (fut, fn, args, kwargs) = item
            if fut.cancelled():
                continue   # 已超时被标记 skip，不执行
            try:
                result = await loop.run_in_executor(
                    self._executor, self._run_with_flag, fn, args, kwargs)
            except Exception as exc:      # fn 抛原异常 → 原样传回调用方的 await
                if not fut.done():
                    fut.set_exception(exc)
            else:
                if not fut.done():
                    fut.set_result(result)

    def _run_with_flag(self, fn, args, kwargs):
        """在执行线程里跑 fn：持 _serial_lock 与 sync invoke 互斥，并打 in_worker 标记。"""
        self._tlocal.in_worker = True
        try:
            with self._serial_lock:
                return fn(*args, **kwargs)
        finally:
            self._tlocal.in_worker = False

    # ── 观测（/api/stats 读）──
    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "depth": dict(self._depth),
            "waits": self._waits,
            "timeouts": self._timeouts,
            "avg_wait_ms": int(self._wait_ms_total / self._waits) if self._waits else 0,
        }

    def reset_metrics(self) -> None:
        """测试用：清空指标（depth/waits/timeouts/平均等待）。"""
        self._depth = {"talk": 0, "review": 0, "compile": 0, "reflect": 0}
        self._waits = 0
        self._timeouts = 0
        self._wait_ms_total = 0


# 模块级单例（任务书 §2.1）—— 由 server lifespan 挂载/卸载；未 start 时 worker 不跑
SCHED = LLMScheduler()

