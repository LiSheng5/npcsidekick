"""G1 决策源扩展回归锚（2026-09-16）：目标真值层接进自主抽签。

《NPC大脑架构》§29.3 G1 / §30.1 第 4 步第三根柱子（开关 `NPC_GOALS`，默认关）。
本文件钉五件事：
  ① **关 = 与旧版逐字节一致**（目标完全不参与决策，连种子都不建）；
  ② 开 = 绑定同一件活的活动目标给该项**抬权**，系数由优先级决定（9→×3 / 5→×1 / 1→×1/3）；
  ③ 开 = routine 里没有的活能靠目标**补成候选**（没绑 action 的目标不瞎开工）；
  ④ 开 = 整链**干完**才推进目标进度（G3 后果 → G2 目标的消费点）；
  ⑤ 目标层任何故障**降级不拖垮**自主循环（T-01 的教训同类：新机制不许带走整帧）。
"""
import random

import pytest

from npc.goal import COMPLETED, GoalQueue
from npc.npc import NPC
from npc.scheduler import (
    _choose_routine_item,
    _dynamic_weight,
    npc_goals,
    tick_round,
)
from npc.world import default_world

ITEM = {"action": "gather", "resource": "木材", "count": 1, "weight": 1}


def _world():
    world = default_world()
    world["actors"] = {}
    world["protagonist"] = {"position": "村庄", "name": "主角"}
    world["_tick"] = 0
    return world


def _npc(pid, routine, world, goals=None):
    persona = {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [],
        "rules": {"replies": {}, "fallback": "嗯。"},
        "routine": routine,
        "goals": goals or {},
    }
    return NPC(persona=persona, world=world, store_dir="npc/store_test")


def _bound_goal(priority=9, target=1):
    """绑定到"采集木材"的目标（游戏无关：绑定写在声明里，引擎不猜）。"""
    return {"砍两根木材": {"target": target, "priority": priority,
                          "action": "gather", "params": {"resource": "木材"}}}


class TestSwitchOffIsZeroChange:
    """关着的时候：目标层一个字都不参与。"""

    def test_goals_ignored_when_off(self, monkeypatch):
        monkeypatch.delenv("NPC_GOALS", raising=False)
        w1, w2 = _world(), _world()
        # 同一个 actor_id（事件字典按 id 做键，得同键才比得出差异）
        plain = _npc("same", [dict(ITEM)], w1)
        withgoals = _npc("same", [dict(ITEM)], w2, goals=_bound_goal())

        seq = []
        for world, npc in ((w1, plain), (w2, withgoals)):
            evs = [tick_round(world, {"same": npc}, rng=random.Random(5)) for _ in range(6)]
            seq.append(evs)

        assert seq[0] == seq[1], "同种子同 routine → 事件序列必须逐字一致"
        # 关着时连种子都不建（零开销的证据）
        assert getattr(withgoals, "_goal_queue", None) is None

    def test_weight_unchanged_when_off(self, monkeypatch):
        monkeypatch.delenv("NPC_GOALS", raising=False)
        world = _world()
        bas = _npc("bas", [], world)                       # 无 goals
        withg = _npc("withg", [], world, goals=_bound_goal())
        assert _dynamic_weight(bas, world, dict(ITEM), 1) == \
            _dynamic_weight(withg, world, dict(ITEM), 1)


class TestWeightBoost:
    """开着的时候：绑定同一件活的目标按优先级抬权（纯数值）。"""

    def test_priority_scales_factor(self, monkeypatch):
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        neutral = _npc("n", [], world)
        w_base = _dynamic_weight(neutral, world, dict(ITEM), 1)   # 无目标 = 基准

        for priority, expected in ((9, 3.0), (5, 1.0), (1, 1 / 3)):
            npc = _npc(f"p{priority}", [], world, goals=_bound_goal(priority=priority))
            got = _dynamic_weight(npc, world, dict(ITEM), 1)
            assert got == pytest.approx(w_base * expected), f"优先级 {priority} 的系数不对"

    def test_unbound_item_is_not_penalised(self, monkeypatch):
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        npc = _npc("u", [], world, goals=_bound_goal())
        other = {"action": "gather", "resource": "浆果", "count": 1, "weight": 1}
        neutral = _npc("v", [], world)
        assert _dynamic_weight(npc, world, other, 1) == _dynamic_weight(neutral, world, other, 1)


class TestGoalCandidates:
    """开着的时候：目标能创造新工作；没绑动作的目标不参与。"""

    def test_goal_creates_candidate_without_routine(self, monkeypatch):
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        npc = _npc("g", [], world, goals=_bound_goal())
        item = _choose_routine_item(npc, world, random.Random(0))
        assert item is not None
        assert (item["action"], item["resource"]) == ("gather", "木材")
        assert item["_goal"], "目标来的候选要带 _goal 标记（便于追踪）"

    def test_unbound_goal_does_not_act(self, monkeypatch):
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        npc = _npc("g2", [], world, goals={"部落安稳过冬": {"target": 1, "priority": 9}})
        assert _choose_routine_item(npc, world, random.Random(0)) is None, \
            "没绑定动作的目标只能看/靠玩家单 —— 引擎不替它猜"

    def test_existing_routine_item_not_duplicated(self, monkeypatch):
        """routine 里已有的活 → 复用原来那项（只抬权），不重复造项。"""
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        routine_item = dict(ITEM)
        npc = _npc("g3", [routine_item], world, goals=_bound_goal())
        picked = _choose_routine_item(npc, world, random.Random(0))
        assert picked is routine_item

    def test_cooldown_blocks_goal_candidate(self, monkeypatch):
        """目标也吃失败冷却：撞过墙的活别连着撞（T-01 同款防线）。"""
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        npc = _npc("g4", [], world, goals=_bound_goal())
        npc._blocked[("gather", "木材")] = world["_tick"] + 5
        assert _choose_routine_item(npc, world, random.Random(0)) is None

    def test_env_read_live(self, monkeypatch):
        """家规：开关现读现切（不用重启、不用重建 NPC）。"""
        monkeypatch.delenv("NPC_GOALS", raising=False)
        world = _world()
        npc = _npc("g5", [], world, goals=_bound_goal())
        assert _choose_routine_item(npc, world, random.Random(0)) is None
        monkeypatch.setenv("NPC_GOALS", "1")
        assert _choose_routine_item(npc, world, random.Random(0)) is not None

    @pytest.mark.parametrize("bad_routine", [None, "", {"gather": 1}, 42])
    def test_bad_routine_shape_does_not_break_goal_mode(self, monkeypatch, bad_routine):
        """开关打开时也要扛得住坏 routine —— 这是打开态跑测试才抓到的真 bug
        （`routine: None` 会走到 `for item in routine` → TypeError，把整帧带走）。"""
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        npc = _npc("g6", [], world, goals=_bound_goal())
        npc.persona["routine"] = bad_routine
        item = _choose_routine_item(npc, world, random.Random(0))
        assert item is not None and item["resource"] == "木材", "坏 routine 不该挡住目标候选"
        tick_round(world, {"g6": npc}, rng=random.Random(0))    # 整帧也得活着


class TestProgressAdvancedByCompletion:
    """整链干完 → 目标进度 +1（G3 后果 → G2 目标）。"""

    def test_completed_activity_advances_goal(self, monkeypatch):
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        npc = _npc("adv", [], world, goals=_bound_goal(target=1))
        assert npc.activity is None
        for _ in range(8):                      # 走→采→走→交付 = 4 步（count=1）
            tick_round(world, {"adv": npc}, rng=random.Random(1))
        q = npc_goals(npc, world)
        g = q.all()[0]
        assert g.progress >= 1 and g.status == COMPLETED

    def test_failed_activity_does_not_advance(self, monkeypatch):
        """失败不推进（失败只是一步没成，不等于目标作废）。"""
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        # 目标要"石头"，但世界只有一个不产石头的地方 → 规划失败
        npc = _npc("fail", [], world, goals={"采石头": {"target": 1,
                                                   "action": "gather", "params": {"resource": "石头"}}})
        world["locations"]["村庄"]["exits"] = {}      # 走不出去 → 规划/执行失败
        for _ in range(4):
            tick_round(world, {"fail": npc}, rng=random.Random(1))
        q = npc_goals(npc, world)
        assert q.all()[0].progress == 0


class TestFaultDegradation:
    """目标层故障 → 降级吞掉，绝不拖垮自主循环。"""

    def test_goal_layer_exception_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("NPC_GOALS", "1")
        world = _world()
        npc = _npc("boom", [dict(ITEM)], world, goals=_bound_goal())

        def _boom(*a, **kw):
            raise RuntimeError("目标层炸了")

        monkeypatch.setattr(GoalQueue, "actions_for", _boom)
        # 权重退回中性、抽签与 tick 都不炸
        neutral = _npc("nb", [], world)
        assert _dynamic_weight(npc, world, dict(ITEM), 1) == \
            _dynamic_weight(neutral, world, dict(ITEM), 1)
        for _ in range(4):
            tick_round(world, {"boom": npc}, rng=random.Random(2))
