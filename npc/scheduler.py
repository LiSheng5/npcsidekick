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


def game_hour(world: Dict) -> int:
    """当前游戏小时(0~23)。游戏端经 /api/talk context 同步的真实时间优先
    (天气/昼夜权重用真实数据,不再猜);无同步时按 tick 自推(60 tick=1小时,同构近似)。"""
    gh = world.get("_game_hour")
    if gh is not None:
        return int(gh)
    return (DAY_START_HOUR + world.get("_tick", 0) // TICKS_PER_GAME_HOUR) % 24


def _is_night(hour: int) -> bool:
    return hour >= 22 or hour < 6   # [PLACEHOLDER] 夜间 22:00~06:00


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
        base = w if isinstance(w, (int, float)) and w > 0 else 1
        # 动态权重(2026-08-22): persona 静态 weight × 耐力 × 昼夜 × 库存缺口
        weights.append(_dynamic_weight(npc, world, item, base))
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
from typing import Optional as _Opt

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
    return os.environ.get("NPC_SCHEDULER", "") not in ("", "0")


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
    async def run(self, priority: int, fn, /, *args, timeout: _Opt[float] = None, **kwargs):
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

    def invoke(self, priority: int, fn, /, *args, timeout: _Opt[float] = None, **kwargs):
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

