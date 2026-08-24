# -*- coding: utf-8 -*-
"""耐力系统 + 做事权重(2026-08-22)。
- world.apply_action 按动作扣耐力;actor 槽位自带 stamina;旧档无键兼容
- observe 体力感知(对话层一致)
- scheduler: rest 恢复;动态权重(耐力/昼夜/库存缺口);game_hour 推算
"""
import random

import pytest

from npc.scheduler import (
    TICKS_PER_GAME_HOUR,
    _choose_routine_item,
    _dynamic_weight,
    game_hour,
    tick_round,
)
from npc.world import STAMINA_MAX, apply_action, default_world, observe, actor_of


class _NPC:
    """轻量 NPC 桩:scheduler 只用 persona/actor_id/_blocked/pending_task。"""

    def __init__(self, actor_id="cang", routine=None):
        self.actor_id = actor_id
        self.persona = {"routine": routine or []}
        self._blocked = {}
        self.pending_task = None
        self.activity = None
        self.state = "idle"

    def remember(self, *a, **kw):
        pass


# ── world:耐力扣减 ──────────────────────────────────────


def test_actor_slot_has_stamina_default():
    w = default_world()
    actor = actor_of(w, "cang")
    assert actor["stamina"] == STAMINA_MAX


def test_legacy_actor_without_stamina_key():
    """旧存档 actor 无 stamina 键 → actor_of 补键,不炸。"""
    w = default_world()
    w["actors"]["cang"] = {"position": "森林", "inventory": {}}   # 手造旧档
    actor = actor_of(w, "cang")
    assert actor["stamina"] == STAMINA_MAX


def test_gather_drains_stamina():
    w = default_world()
    actor_of(w, "cang")
    w["actors"]["cang"]["position"] = "森林"
    before = w["actors"]["cang"]["stamina"]
    _, ok, _ = apply_action(w, "gather", {"resource": "木材"}, who="cang")
    assert ok
    assert w["actors"]["cang"]["stamina"] == pytest.approx(before - 10)


def test_move_drains_less_than_gather():
    w = default_world()
    actor_of(w, "ali")
    w["actors"]["ali"]["position"] = "村庄"
    s0 = w["actors"]["ali"]["stamina"]
    apply_action(w, "move", {"dest": "森林"}, who="ali")
    s1 = w["actors"]["ali"]["stamina"]
    assert s1 == pytest.approx(s0 - 4)


def test_failed_action_does_not_drain():
    """失败的 gather(地点无资源)不扣耐力。"""
    w = default_world()
    actor_of(w, "cang")
    w["actors"]["cang"]["position"] = "村庄"   # 村庄无木材
    s0 = w["actors"]["cang"]["stamina"]
    _, ok, _ = apply_action(w, "gather", {"resource": "木材"}, who="cang")
    assert not ok
    assert w["actors"]["cang"]["stamina"] == s0


def test_stamina_floors_at_zero():
    w = default_world()
    actor_of(w, "cang")
    w["actors"]["cang"]["position"] = "森林"
    w["actors"]["cang"]["stamina"] = 3.0
    _, ok, _ = apply_action(w, "gather", {"resource": "木材"}, who="cang")
    assert ok
    assert w["actors"]["cang"]["stamina"] == 0.0


def test_say_does_not_drain():
    w = default_world()
    actor_of(w, "cang")
    s0 = w["actors"]["cang"]["stamina"]
    _, ok, _ = apply_action(w, "say", {"text": "嗯。"}, who="cang")
    assert ok
    assert w["actors"]["cang"]["stamina"] == s0


# ── world:observe 体力感知 ──────────────────────────────


def test_observe_reports_exhaustion():
    w = default_world()
    actor_of(w, "cang")
    w["actors"]["cang"]["stamina"] = 20.0
    text = observe(w, "cang")
    assert "歇" in text


def test_observe_silent_when_fresh():
    w = default_world()
    actor_of(w, "cang")
    text = observe(w, "cang")
    assert "歇" not in text


# ── scheduler:昼夜推算 ─────────────────────────────────


def test_game_hour_starts_morning():
    w = default_world()
    assert game_hour(w) == 8


def test_game_hour_advances():
    w = default_world()
    w["_tick"] = TICKS_PER_GAME_HOUR   # 1 小时后
    assert game_hour(w) == 9


def test_game_hour_wraps_midnight():
    w = default_world()
    w["_tick"] = TICKS_PER_GAME_HOUR * 17   # 8 + 17 = 25 → 1 点
    assert game_hour(w) == 1


# ── scheduler:rest 恢复 ─────────────────────────────────


def test_rest_step_recovers_stamina():
    """tick_round 走 rest 步骤 → 耐力回升。"""
    w = default_world()
    npc = _NPC("cang", routine=[{"action": "rest", "ticks": 2, "weight": 1}])
    actor_of(w, "cang")
    w["actors"]["cang"]["stamina"] = 10.0
    tick_round(w, {"cang": npc})
    assert w["actors"]["cang"]["stamina"] > 10.0


def test_stamina_recovers_via_full_loop():
    """力竭 NPC 全程跑 rest 日常 → 数 tick 后耐力显著回升。"""
    w = default_world()
    npc = _NPC("cang", routine=[{"action": "rest", "ticks": 4, "weight": 1}])
    actor_of(w, "cang")
    w["actors"]["cang"]["stamina"] = 5.0
    for _ in range(6):
        tick_round(w, {"cang": npc}, rng=random.Random(1))
    assert w["actors"]["cang"]["stamina"] >= 60.0


# ── scheduler:动态权重 ─────────────────────────────────


def test_dynamic_weight_exhausted_boosts_rest():
    w = default_world()
    npc = _NPC("cang")
    actor_of(w, "cang")
    w["actors"]["cang"]["stamina"] = 10.0   # 力竭
    w["_tick"] = TICKS_PER_GAME_HOUR * 2    # 10 点白天
    rest_w = _dynamic_weight(npc, w, {"action": "rest"}, 1.0)
    assert rest_w >= 8.0   # 力竭 ×8


def test_dynamic_weight_exhausted_suppresses_gather():
    w = default_world()
    npc = _NPC("cang")
    actor_of(w, "cang")
    w["actors"]["cang"]["stamina"] = 10.0
    w["_tick"] = TICKS_PER_GAME_HOUR * 2
    gather_w = _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 1.0)
    assert gather_w <= 0.2


def test_dynamic_weight_fresh_unchanged_daytime():
    """满耐力白天:权重 = 库存缺口因子(木材缺口<5 → ×1.6)。"""
    w = default_world()
    npc = _NPC("cang")
    actor_of(w, "cang")
    w["_tick"] = TICKS_PER_GAME_HOUR * 2
    gather_w = _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 2.0)
    assert gather_w == pytest.approx(2.0 * 1.6)


def test_dynamic_weight_night_suppresses_gather():
    w = default_world()
    npc = _NPC("cang")
    actor_of(w, "cang")
    w["_tick"] = TICKS_PER_GAME_HOUR * 15   # 8+15=23 点深夜
    gather_w = _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 2.0)
    assert gather_w < 2.0 * 1.6   # 比白天同条件低


def test_dynamic_weight_stockpile_suppresses_gather():
    """delivered 已 20+ → 满仓降权。"""
    w = default_world()
    npc = _NPC("cang")
    actor_of(w, "cang")
    w["delivered"]["木材"] = 30
    w["_tick"] = TICKS_PER_GAME_HOUR * 2
    gather_w = _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 2.0)
    assert gather_w < 2.0


def test_choose_routine_prefers_rest_when_exhausted():
    """力竭时统计上显著偏向 rest(固定种子跑 200 次选日常)。"""
    w = default_world()
    routine = [
        {"action": "gather", "resource": "木材", "count": 1, "weight": 5},   # 静态权重很高
        {"action": "rest", "ticks": 3, "weight": 1},
    ]
    rng = random.Random(42)
    # 满耐力基线:高权重 gather 应占多数
    npc = _NPC("cang", routine=routine)
    actor_of(w, "cang")
    fresh_gather = 0
    for _ in range(200):
        item = _choose_routine_item(npc, w, rng)
        if item and item["action"] == "gather":
            fresh_gather += 1
    # 力竭:rest 应反超
    w["actors"]["cang"]["stamina"] = 10.0
    tired_gather = 0
    for _ in range(200):
        item = _choose_routine_item(npc, w, rng)
        if item and item["action"] == "gather":
            tired_gather += 1
    assert fresh_gather > 100               # 基线:gather 多数
    assert tired_gather < fresh_gather / 2  # 力竭后 gather 显著减少
