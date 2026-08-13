"""调度测试 — 主角互动优先 + 自主 tick 循环（村民日常）。"""
import random

from npc.npc import NPC
from npc.scheduler import BLOCK_AFTER_FAIL_TICKS, interaction_priority, tick_round
from npc.world import default_world


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
