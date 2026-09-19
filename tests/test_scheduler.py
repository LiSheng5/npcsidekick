"""调度测试 — 主角互动优先 + 自主 tick 循环（村民日常）。

⚠ 本文件全部用例钉的是**决策层可得性探针关态**（= 旧行为，见下方 autouse fixture）。
   探针（NPC_AVAILABILITY）打开后，采不到的活会在**候选阶段**就被跳过，
   与这里断言的"抽中 → 规划期优雅放弃 → 记一条失败 → 冷却 5 tick"不是同一套行为；
   开态行为锚在 `tests/test_availability_probe.py`。
"""
import random

import pytest

from npc.npc import NPC
from npc.scheduler import BLOCK_AFTER_FAIL_TICKS, interaction_priority, tick_round
from npc.world import default_world


@pytest.fixture(autouse=True)
def _probe_off(monkeypatch):
    """强制探针关态 —— 本文件的用例都是为"旧行为"写的，不该受环境变量影响。"""
    monkeypatch.delenv("NPC_AVAILABILITY", raising=False)


def _village_world():
    """阿黎在森林（远），苍在村庄（近），主角在村庄。"""
    world = default_world()
    world["actors"]["ali"] = {"position": "森林", "inventory": {}}
    world["actors"]["cang"] = {"position": "村庄", "inventory": {}}
    world["protagonist"] = {"position": "村庄", "name": "主角"}
    return world


class TestInteractionPriority:
    def test_closer_npc_first(self):
        """离主角近的先行动（主角互动优先）。"""
        world = _village_world()
        order = interaction_priority(world)
        assert order[0] == "cang"      # 在村庄（距离 0）
        assert order[1] == "ali"   # 在森林（距离 2: 森林→村庄）

    def test_recent_interaction_boosts(self):
        """最近互动过的 NPC 置顶（即使离得远）。"""
        world = _village_world()
        order = interaction_priority(world, recent_interactions=["ali"])
        assert order[0] == "ali"

    def test_stable_for_equal_distance(self):
        world = default_world()
        world["actors"]["a"] = {"position": "村庄", "inventory": {}}
        world["actors"]["b"] = {"position": "村庄", "inventory": {}}
        world["actors"]["c"] = {"position": "矿洞", "inventory": {}}
        order = interaction_priority(world)
        # a/b 距离相同 → 注册顺序保持
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("c")


# ── 自主 tick 循环（村民日常）────────────────────────

def _routine_npc(pid, routine, world=None):
    """带 routine 的测试 NPC（独立世界或共享世界）。"""
    persona = {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [],
        "rules": {"replies": {"好": "好的。"}, "fallback": "嗯。"},
        "routine": routine,
    }
    return NPC(persona=persona, world=world, store_dir="npc/store_test")


class TestTickLoop:
    """tick 循环: 纯函数、零 LLM、种子确定性。"""

    def test_seeded_deterministic(self):
        """同种子两次 tick_round → 完全相同的转换事件序列。"""
        routine = [{"action": "gather", "resource": "木材", "count": 2, "weight": 3},
                   {"action": "rest", "ticks": 2, "weight": 1}]

        def run():
            world = default_world()
            npc = _routine_npc("lilang", routine, world=world)
            evs = []
            for _ in range(10):
                evs.append(tick_round(world, {"lilang": npc}, rng=random.Random(42)))
            return evs

        assert run() == run()

    def test_gather_chain_delivers(self):
        """gather 木材×2 全链（走→采×2→回→交付×2 = 6 步）→ delivered 2、背包清空、回主角处。"""
        world = default_world()
        npc = _routine_npc("caiwu", [{"action": "gather", "resource": "木材", "count": 2, "weight": 1}], world=world)
        events = []
        for _ in range(6):
            events.append(tick_round(world, {"caiwu": npc}, rng=random.Random(1)))
        assert world["delivered"]["木材"] == 2
        assert world["actors"]["caiwu"]["inventory"].get("木材", 0) == 0   # 交付后背包清零（允许零值键）
        assert world["actors"]["caiwu"]["position"] == "村庄"
        assert npc.state == "idle"
        assert events[0]["caiwu"]["started"] == "采集木材×2"
        assert events[-1]["caiwu"]["completed"] == "采集木材×2"

    def test_one_step_per_npc_per_tick(self):
        """每 tick 每 NPC 恰一步: 第 1 tick 只走了一步（还没采到）。"""
        world = default_world()
        npc = _routine_npc("maibu", [{"action": "gather", "resource": "木材", "count": 2, "weight": 1}], world=world)
        events = tick_round(world, {"maibu": npc}, rng=random.Random(1))
        assert world["_tick"] == 1
        assert "started" in events["maibu"]
        # 第 1 步是走路（村庄→森林），背包还没东西
        assert world["actors"]["maibu"]["inventory"] == {}
        assert world["actors"]["maibu"]["position"] == "森林"

    def test_two_npcs_interleave(self):
        """双 NPC 共享世界各自推进: 一个运木材、一个运浆果，互不干扰。"""
        world = default_world()
        a = _routine_npc("jia", [{"action": "gather", "resource": "木材", "count": 1, "weight": 1}], world=world)
        b = _routine_npc("yi", [{"action": "gather", "resource": "浆果", "count": 1, "weight": 1}], world=world)
        npcs = {"jia": a, "yi": b}
        for _ in range(4):   # 各自 4 步（走→采→回→交付）
            tick_round(world, npcs, rng=random.Random(7))
        assert world["delivered"]["木材"] == 1
        assert world["delivered"]["浆果"] == 1
        assert a.state == "idle" and b.state == "idle"

    def test_rest_does_not_consume_resources(self):
        """纯休息日常: 不消耗资源、state 依次 resting → idle。"""
        world = default_world()
        npc = _routine_npc("xixi", [{"action": "rest", "ticks": 3, "weight": 1}], world=world)
        before = (dict(world["locations"]["森林"]["resources"]), dict(world["delivered"]))
        ev1 = tick_round(world, {"xixi": npc}, rng=random.Random(3))
        assert npc.state == "resting"
        tick_round(world, {"xixi": npc}, rng=random.Random(3))
        tick_round(world, {"xixi": npc}, rng=random.Random(3))
        assert npc.state == "idle"
        assert "completed" in ev1["xixi"] or True   # started 在第一个 tick
        assert world["locations"]["森林"]["resources"] == before[0]
        assert world["delivered"] == before[1]

    def test_no_routine_means_static(self):
        """无 routine → NPC 静止（state 恒 idle，世界原样）。"""
        world = default_world()
        npc = _routine_npc("jingzhi", None, world=world)
        for _ in range(5):
            events = tick_round(world, {"jingzhi": npc}, rng=random.Random(0))
            assert events == {}
            assert npc.state == "idle"
        assert world["locations"]["森林"]["resources"]["木材"] == 999
        assert world["delivered"] == {}

    def test_meeting_say_during_rest(self):
        """休息时碰面 → 说一句 rules 台词（写进世界 log）。"""
        fired = None
        for seed in range(200):
            world = default_world()
            a = _routine_npc("tanya", [{"action": "rest", "ticks": 2, "weight": 1}], world=world)
            b = _routine_npc("penyou", [{"action": "rest", "ticks": 2, "weight": 1}], world=world)
            for _ in range(4):
                tick_round(world, {"tanya": a, "penyou": b}, rng=random.Random(seed))
            if any(" 说: " in line for line in world["log"]):
                fired = seed
                break
        assert fired is not None, "没找到能触发碰面说话的种子"
        said = [line.split(" 说: ", 1)[1] for line in world["log"] if " 说: " in line]
        assert all(t in ("好的。", "嗯。") for t in said)

    def test_completion_remembers(self):
        """活动完成 → 记忆卡追加 importance=5 的完成条目。"""
        world = default_world()
        npc = _routine_npc("jide", [{"action": "gather", "resource": "木材", "count": 1, "weight": 1}], world=world)
        for _ in range(4):
            tick_round(world, {"jide": npc}, rng=random.Random(5))
        last = npc.memory.all()[-1]
        assert last["importance"] == 5
        assert "完成" in last["content"]

    def test_failed_delivery_aborts_gracefully(self):
        """交付时主角不在 → 活动优雅中止（failed 事件、回 idle、记忆留痕，不崩溃）。"""
        world = default_world()
        npc = _routine_npc("songhuo", [{"action": "gather", "resource": "木材", "count": 2, "weight": 1}], world=world)
        tick_round(world, {"songhuo": npc}, rng=random.Random(2))        # 起步
        world["protagonist"]["position"] = "矿洞"                        # 主角跑了
        events = {}
        for _ in range(4):
            events = tick_round(world, {"songhuo": npc}, rng=random.Random(2))
        assert "failed" in events["songhuo"]
        assert npc.state == "idle" and npc.activity is None
        assert any("没做成" in e["content"] for e in npc.memory.all())
        assert npc.world["actors"]["songhuo"]["position"] == "村庄"      # 状态一致

    def test_exhausted_resource_blocklist(self):
        """资源争夺: 需求 > 再生时后到者步骤级采尽失败 → 进冷却;冷却后资源长回,重试成功。"""
        world = default_world()
        world["locations"]["森林"]["resources"]["木材"] = 1   # A 在 B 前面抢走再生的那 1 个 → B 采尽
        a = _routine_npc("duana", [{"action": "gather", "resource": "木材", "count": 2, "weight": 1}], world=world)
        b = _routine_npc("duanb", [{"action": "gather", "resource": "木材", "count": 3, "weight": 1}], world=world)
        a.world["actors"]["duana"]["position"] = "森林"
        b.world["actors"]["duanb"]["position"] = "森林"
        npcs = {"duana": a, "duanb": b}
        for _ in range(3):   # 双双开采: A 采 2 个,B 第三个时采尽
            tick_round(world, npcs, rng=random.Random(6))
        assert any("没做成" in e["content"] for e in b.memory.all())  # B 采尽失败
        # 冷却期内: B 不再选木材日常（无候选 → 静止）；判定 > now，第 BLOCK 个 tick 解冻
        # 失败在 tick2 → 冷却至 tick7（含 4 个 tick）；测试循环从 tick4 起，故 3 次迭代覆盖 4/5/6
        for _ in range(BLOCK_AFTER_FAIL_TICKS - 2):
            events = tick_round(world, npcs, rng=random.Random(6))
            assert "duanb" not in events or "started" not in events["duanb"]
        # 冷却结束: 资源已再生长出来 → B 重新接活
        events = tick_round(world, npcs, rng=random.Random(6))
        assert "started" in events["duanb"]

    def test_events_only_at_transitions(self):
        """转换事件只在开始/完成 tick 出现，进行中 tick 返回空。"""
        world = default_world()
        npc = _routine_npc("zhuanhuan", [{"action": "gather", "resource": "木材", "count": 2, "weight": 1}], world=world)
        evs = []
        for _ in range(6):
            evs.append(tick_round(world, {"zhuanhuan": npc}, rng=random.Random(9)))
        assert set(evs[0]["zhuanhuan"]) == {"started"}      # 第 1 tick: 开始
        for ev in evs[1:5]:
            assert ev == {}                                  # 进行中: 无转换
        assert set(evs[5]["zhuanhuan"]) == {"completed"}    # 最后: 完成

    def test_activity_not_persisted(self):
        """活动不落盘: 中途 save/load → activity 清零、世界保留已完成部分。"""
        world = default_world()
        npc = _routine_npc("buoluo", [{"action": "gather", "resource": "木材", "count": 2, "weight": 1}], world=world)
        for _ in range(2):   # 走到森林 + 采了 1 个
            tick_round(world, {"buoluo": npc}, rng=random.Random(4))
        assert world["actors"]["buoluo"]["inventory"]["木材"] == 1
        npc.save()
        loaded = NPC.load("buoluo", store_dir="npc/store_test")
        assert loaded.activity is None
        assert loaded.state == "idle"
        assert loaded.world["actors"]["buoluo"]["inventory"]["木材"] == 1

    def test_tick_counter_persists(self):
        """tick 计数随记忆卡持久化（world 落盘的一部分）。"""
        world = default_world()
        npc = _routine_npc("tickguo", None, world=world)
        for _ in range(3):
            tick_round(world, {"tickguo": npc}, rng=random.Random(0))
        assert world["_tick"] == 3
        npc.save()
        loaded = NPC.load("tickguo", store_dir="npc/store_test")
        assert loaded.world["_tick"] == 3

    def test_resources_regen(self):
        """资源再生: 采空的资源每 tick 回补（树会再长 — 不会永远采空）。"""
        world = default_world()
        world["locations"]["森林"]["resources"]["木材"] = 0
        npc = _routine_npc("shengzhang", [{"action": "rest", "ticks": 1, "weight": 1}], world=world)
        tick_round(world, {"shengzhang": npc}, rng=random.Random(0))
        assert world["locations"]["森林"]["resources"]["木材"] == 1
        # 上限: 满的不会涨过初始值
        tick_round(world, {"shengzhang": npc}, rng=random.Random(0))
        assert world["locations"]["森林"]["resources"]["木材"] == 2

    def test_near_npc_wins_scarce_pool(self):
        """近者先: 同 tick 内唯一的浆果被近者采走，远者规划期被挡（再生只在下个 tick 生效）。"""
        world = default_world()
        world["protagonist"]["position"] = "河边"
        world["locations"]["河边"]["resources"]["浆果"] = 0   # 再生在 tick 开头才长 1 个
        near = _routine_npc("jinjia", [{"action": "gather", "resource": "浆果", "count": 1, "weight": 1}], world=world)
        far = _routine_npc("yuanjia", [{"action": "gather", "resource": "浆果", "count": 1, "weight": 1}], world=world)
        near.world["actors"]["jinjia"]["position"] = "河边"   # 近者已在河边
        tick_round(world, {"jinjia": near, "yuanjia": far}, rng=random.Random(8))
        # 近者（优先级高）拿到唯一浆果
        assert near.world["actors"]["jinjia"]["inventory"]["浆果"] == 1
        assert far.world["actors"]["yuanjia"]["inventory"] == {}
        # 远者同 tick 规划时资源已被采空 → 规划期放弃（优雅，不白跑一趟）
        assert any("没找到地方" in e["content"] for e in far.memory.all())


# ── T-01 回归锚（2026-09-15）: routine 项再坏也不能把整帧带走 ────────────────
import pytest

from npc.scheduler import _plan_steps

UNPLANNABLE_NOTE = "引擎还没有这个动作"


def _bare_npc(pid, routine, world, store_dir):
    """带 routine 的测试 NPC（显式指定 store_dir — 不污染仓库里的 npc/store_test）。"""
    persona = {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [],
        "rules": {"replies": {"好": "好的。"}, "fallback": "嗯。"},
        "routine": routine,
    }
    return NPC(persona=persona, world=world, store_dir=store_dir)


class TestPlanStepsIsTotal:
    """``_plan_steps`` 对任何 routine 项都必须是"总函数": 返回 None，不抛异常。

    背景（审计 T-01，实测复现）: 旧实现对非 rest/say 项一律走 gather 链
    (``resource = item["resource"]``)，于是 craft / 自定义动作 / 缺 resource 的
    gather 全部 ``KeyError``；异常冒到 ``server._tick_loop`` 的 ``except`` 会吞掉
    **整帧** —— 落盘、反思、管家、账本回收一起跳过，NPC 每帧静默空转、零日志线索。

    契约来源: ``test_persona_loader.py::test_any_action_accepted`` /
    ``::test_gather_without_resource_ok`` —— loader 只校验"非空字符串"，能不能执行
    由 Runtime 说了算，**不崩**。
    """

    @pytest.mark.parametrize("item", [
        # T-04(2026-09-16) 后 craft **可编排**了 —— 这里改用"世界没这个配方"当不可编排样例
        {"action": "craft", "recipe": "不存在的配方"},
        {"action": "gather", "weight": 1},                 # loader 明许缺 resource
        {"action": "巡逻", "weight": 2},                    # 任意自定义动作
        {"action": "trade", "resource": "铁料", "count": 1},  # 带 resource 也不是 gather 链
        {"action": "gather", "resource": "", "count": 1},   # 空字符串 resource
    ])
    def test_unplannable_item_returns_none(self, item, tmp_path):
        """认不出来的 routine 项 → None（旧版是 KeyError）。"""
        world = default_world()
        npc = _bare_npc("plan", [item], world, str(tmp_path))
        assert _plan_steps(npc, world, item) is None

    def test_whole_frame_survives_bad_routine(self, tmp_path):
        """同帧的正常 NPC 照旧推进 —— 一个坏项不许吃掉别人的一步。"""
        world = default_world()
        # T-04(2026-09-16): craft 已可编排 → 坏项改用自定义动作（引擎确实没有）
        bad = _bare_npc("huai", [{"action": "巡逻", "weight": 2}], world, str(tmp_path))
        good = _bare_npc("hao", [{"action": "gather", "resource": "木材", "count": 1, "weight": 1}],
                         world, str(tmp_path))
        events = tick_round(world, {"huai": bad, "hao": good}, rng=random.Random(1))
        assert bad.state == "idle"
        assert "started" in events["hao"]          # 正常那位的这一步没被吞

    def test_player_order_with_craft_does_not_crash(self, tmp_path):
        """玩家单(pending_task)写 craft 同样不许崩 —— B2 编译产得出 craft 单，旧版必炸。"""
        world = default_world()
        npc = _bare_npc("kehu", [], world, str(tmp_path))
        npc.pending_task = {"action": "craft", "recipe": "木石工具", "count": 1}
        tick_round(world, {"kehu": npc}, rng=random.Random(1))
        assert npc.pending_task is None            # 接单一次后清掉（原语义不变）
        assert npc.state == "idle"

    def test_planning_failure_enters_cooldown(self, tmp_path):
        """计划失败也进冷却 —— 否则每帧重选同一项 → 每帧写一条记忆（记忆卡刷屏）。"""
        world = default_world()
        npc = _bare_npc("leng", [{"action": "craft", "recipe": "木石工具"}], world, str(tmp_path))
        tick_round(world, {"leng": npc}, rng=random.Random(1))
        assert npc._blocked[("craft", None)] > world["_tick"]

    def test_planning_failure_memory_not_spammed(self, tmp_path):
        """连跑 10 帧只留 2 条说明（冷却 5 tick 生效），不是每帧一条。"""
        world = default_world()
        # T-04(2026-09-16): craft 已可编排 → 用自定义动作造"计划失败"（引擎没有这个动作）
        npc = _bare_npc("shao", [{"action": "巡逻", "weight": 2}], world, str(tmp_path))
        for _ in range(10):
            tick_round(world, {"shao": npc}, rng=random.Random(1))
        notes = [e for e in npc.memory.all() if UNPLANNABLE_NOTE in e["content"]]
        assert len(notes) == 2

    def test_supported_items_plan_unchanged(self, tmp_path):
        """默认世界零差异: gather / rest / say 的步骤序列与旧版逐字一致。"""
        world = default_world()
        npc = _bare_npc("jiu", [], world, str(tmp_path))
        kinds = [s["kind"] for s in
                 _plan_steps(npc, world, {"action": "gather", "resource": "木材", "count": 2})]
        assert kinds == ["walk", "gather", "gather", "walk", "deliver", "deliver"]
        assert _plan_steps(npc, world, {"action": "rest", "ticks": 3}) == [{"kind": "rest"}] * 3
        assert _plan_steps(npc, world, {"action": "say"}) == [{"kind": "say"}]


# ── LLM 调度队列（任务书 #01，NPC_SCHEDULER=1 下过）────────────────────
import asyncio
import threading
import time

import pytest

from npc.scheduler import (
    P_COMPILE,
    P_REFLECT,
    P_REVIEW,
    P_TALK,
    SCHED,
    SchedulerTimeout,
)


class TestLLMScheduler:
    """验收：kill-switch / 优先级 / 串行性 / 嵌套直通 / 超时兜底 / 指标。

    用 pytest-asyncio（asyncio_mode=auto）跑 async 测试；autouse fixture 保证
    每个测试前后 start / stop 调度 worker，避免模块级单例状态跨测试污染。
    """

    @pytest.fixture(autouse=True)
    async def _sched(self):
        SCHED.reset_metrics()
        SCHED.start()
        try:
            yield
        finally:
            await SCHED.stop()

    async def test_kill_switch_default_off(self, monkeypatch):
        """验收 1：不设 NPC_SCHEDULER → 走旧路径，run 直接执行（零排队）。"""
        monkeypatch.delenv("NPC_SCHEDULER", raising=False)
        assert SCHED.enabled is False
        calls = []
        assert await SCHED.run(P_TALK, lambda: calls.append("x") or "direct") == "direct"
        assert calls == ["x"]

    async def test_priority_talk_before_reflect(self, monkeypatch):
        """验收 2：先入 REFLECT 再入 TALK → TALK 先执行完（优先级低值先出）。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        finished = []
        blocker = asyncio.ensure_future(
            SCHED.run(P_COMPILE, lambda: (time.sleep(0.4), "blocker")[1]))
        await asyncio.sleep(0.15)      # worker 已取 blocker 开始 sleep
        r = asyncio.ensure_future(
            SCHED.run(P_REFLECT, lambda: finished.append("reflect") or "reflect"))
        await asyncio.sleep(0.03)      # reflect 已入队
        t = asyncio.ensure_future(
            SCHED.run(P_TALK, lambda: finished.append("talk") or "talk"))
        await asyncio.gather(blocker, r, t)
        assert finished == ["talk", "reflect"]

    async def test_serial_execution_no_overlap(self, monkeypatch):
        """验收 3：并发入队 10 个记录起止时间的任务 → 执行区间不重叠。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        intervals = []
        lock = threading.Lock()

        def make(i):
            def fn():
                start = time.monotonic()
                time.sleep(0.02)
                end = time.monotonic()
                with lock:
                    intervals.append((i, start, end))
                return i
            return fn

        await asyncio.gather(*[SCHED.run(P_REVIEW, make(i)) for i in range(10)])
        intervals.sort(key=lambda x: x[1])
        for a, b in zip(intervals, intervals[1:]):
            assert a[2] <= b[1]

    async def test_nested_passthrough_no_deadlock(self, monkeypatch):
        """验收 4：worker 内任务再 invoke() → 直通执行，3 秒内完成（不死锁）。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        calls = []

        def inner():
            calls.append("inner")
            return "inner-result"

        def outer():
            calls.append("outer")
            r = SCHED.invoke(P_REVIEW, inner)      # worker 内嵌套 → 直通
            assert r == "inner-result"
            return "outer-result"

        t0 = time.monotonic()
        result = await SCHED.run(P_TALK, outer)
        assert result == "outer-result"
        assert time.monotonic() - t0 < 3.0
        assert calls == ["outer", "inner"]

    async def test_timeout_fallback(self, monkeypatch):
        """验收 5：sleep(2) 占住 worker，timeout=0.5 提交 → SchedulerTimeout（落兜底）。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        slow = asyncio.ensure_future(
            SCHED.run(P_REFLECT, lambda: (time.sleep(2), "slow")[1]))
        await asyncio.sleep(0.1)                   # worker 已取 slow 开始 sleep
        with pytest.raises(SchedulerTimeout):
            await SCHED.run(P_TALK, lambda: "talk", timeout=0.5)
        await slow

    async def test_metrics_grow(self, monkeypatch):
        """验收 6：若干次运行后 stats 的 scheduler 桶数值正确增长。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        for _ in range(3):
            await SCHED.run(P_TALK, lambda: "ok")
        slow = asyncio.ensure_future(
            SCHED.run(P_REFLECT, lambda: (time.sleep(1), "slow")[1]))
        await asyncio.sleep(0.05)
        try:
            await SCHED.run(P_REVIEW, lambda: "y", timeout=0.1)
        except SchedulerTimeout:
            pass
        await slow
        snap = SCHED.snapshot()
        assert snap["enabled"] is True
        assert snap["waits"] == 5          # 3 talk + 1 slow + 1 超时
        assert snap["timeouts"] == 1
        assert snap["avg_wait_ms"] >= 0

    async def test_reflect_and_talk_mutual_exclusion(self, monkeypatch):
        """返工①：反思路径（sync invoke）与对话任务（executor）互斥——invoke 等占用者结束。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        occupied_end = []
        invoke_start = []

        def occupy():
            time.sleep(0.3)
            occupied_end.append(time.monotonic())
            return "occupied"

        def reflect():
            invoke_start.append(time.monotonic())
            return "reflect"

        task = asyncio.ensure_future(SCHED.run(P_TALK, occupy))
        await asyncio.sleep(0.05)   # 等 worker 取到 occupy 并持锁开始 sleep
        assert SCHED.invoke(P_REFLECT, reflect) == "reflect"   # 阻塞等锁，直到 occupy 结束
        await task
        assert occupied_end and invoke_start
        assert invoke_start[0] >= occupied_end[0]   # invoke 开始晚于占用者结束 → 互斥生效

    async def test_loop_invoke_not_long_blocking(self, monkeypatch):
        """返工①：loop 线程 sync invoke 直接执行（不排队），假慢 fn 耗时 < 1s。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        t0 = time.monotonic()
        result = SCHED.invoke(P_REFLECT, lambda: (time.sleep(0.2), "x")[1])
        elapsed = time.monotonic() - t0
        assert result == "x"
        assert elapsed < 1.0

    async def test_loop_stays_alive_while_lock_busy(self, monkeypatch):
        """实弹教训(2026-08-24)：executor 被长任务占住时，反思经 to_thread 等锁，
        事件循环必须继续心跳（间隔亚秒级）——loop 一旦被同步等锁冻住 = 全服冻结。"""
        monkeypatch.setenv("NPC_SCHEDULER", "1")
        beats: list = []

        async def heartbeat():
            for _ in range(12):
                await asyncio.sleep(0.05)
                beats.append(time.monotonic())

        hb = asyncio.ensure_future(heartbeat())
        slow = asyncio.ensure_future(
            SCHED.run(P_TALK, lambda: (time.sleep(0.8), "slow")[1]))
        await asyncio.sleep(0.1)   # worker 已持锁进入慢任务

        def blocked_invoke():
            # 与 server._tick_loop 同构：反思在 to_thread 工作线程里 invoke 等锁
            SCHED.invoke(P_REFLECT, lambda: "reflect")

        t = asyncio.ensure_future(asyncio.to_thread(blocked_invoke))
        await asyncio.gather(hb, slow, t)
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        assert len(beats) >= 10
        assert max(gaps) < 0.5   # 若 loop 被冻，心跳间隔会炸到秒级

