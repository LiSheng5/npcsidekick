# -*- coding: utf-8 -*-
"""游戏→大脑世界同步(2026-08-22): /api/talk context 直通。
- _apply_context: weather/game_hour/player_pos 直写世界扩展键,坏值容错
- game_hour: 同步的真实时间优先,无同步回退 tick 自推
- _dynamic_weight: 天气因子(雨=干活降权休息升权,节庆=少干活少躺着)
- observe: 天气/深夜感知(对话层一致性)
"""
import pytest

from npc.scheduler import TICKS_PER_GAME_HOUR, _dynamic_weight, game_hour
from npc.server import _apply_context
from npc.world import default_world, observe, actor_of


class _NPC:
    def __init__(self, actor_id="cang"):
        self.actor_id = actor_id
        self.persona = {}


# ── _apply_context ─────────────────────────────────────


def test_apply_context_writes_all_keys():
    w = default_world()
    _apply_context(w, {"weather": "rain", "game_hour": 21, "player_pos": "(120, 340)"})
    assert w["_weather"] == "rain"
    assert w["_game_hour"] == 21
    assert w["_player_pos"] == "(120, 340)"
    assert "_context_at" in w


def test_apply_context_none_and_empty_safe():
    w = default_world()
    _apply_context(w, None)
    _apply_context(w, {})
    assert "_weather" not in w   # 什么都不写


def test_apply_context_tolerates_bad_hour():
    """game_hour 手滑传字符串 → 静默忽略,不炸。"""
    w = default_world()
    _apply_context(w, {"game_hour": "abc"})
    assert "_game_hour" not in w


def test_apply_context_hour_wraps_and_neg():
    w = default_world()
    _apply_context(w, {"game_hour": 25})
    assert w["_game_hour"] == 1


# ── game_hour:真实时间优先 ─────────────────────────────


def test_game_hour_prefers_synced():
    w = default_world()
    w["_game_hour"] = 23
    assert game_hour(w) == 23


def test_game_hour_falls_back_to_tick():
    w = default_world()
    w["_tick"] = TICKS_PER_GAME_HOUR * 5   # 8+5=13 点
    assert game_hour(w) == 13


# ── 天气进动态权重 ─────────────────────────────────────


def test_rain_suppresses_gather():
    w = default_world()
    npc = _NPC()
    actor_of(w, "cang")
    w["_tick"] = TICKS_PER_GAME_HOUR * 2   # 白天,排除昼夜因子
    sunny = _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 2.0)
    w["_weather"] = "rain"
    rainy = _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 2.0)
    assert rainy < sunny


def test_rain_boosts_rest():
    w = default_world()
    npc = _NPC()
    actor_of(w, "cang")
    w["_tick"] = TICKS_PER_GAME_HOUR * 2
    sunny = _dynamic_weight(npc, w, {"action": "rest"}, 2.0)
    w["_weather"] = "rain"
    rainy = _dynamic_weight(npc, w, {"action": "rest"}, 2.0)
    assert rainy > sunny


def test_festival_suppresses_work_and_rest():
    w = default_world()
    npc = _NPC()
    actor_of(w, "cang")
    w["_tick"] = TICKS_PER_GAME_HOUR * 2
    base_gather = _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 2.0)
    base_rest = _dynamic_weight(npc, w, {"action": "rest"}, 2.0)
    w["_weather"] = "festival"
    assert _dynamic_weight(npc, w, {"action": "gather", "resource": "木材"}, 2.0) < base_gather
    assert _dynamic_weight(npc, w, {"action": "rest"}, 2.0) < base_rest


# ── observe:天气/深夜感知 ──────────────────────────────


def test_observe_reports_rain():
    w = default_world()
    actor_of(w, "cang")
    w["_weather"] = "rain"
    assert "雨" in observe(w, "cang")


def test_observe_reports_festival():
    w = default_world()
    actor_of(w, "cang")
    w["_weather"] = "festival"
    assert "节庆" in observe(w, "cang")


def test_observe_reports_late_night():
    w = default_world()
    actor_of(w, "cang")
    w["_game_hour"] = 23
    assert "夜" in observe(w, "cang")


def test_observe_silent_without_sync():
    """未同步(纯文本世界):无天气无时间感知,静默自洽。"""
    w = default_world()
    actor_of(w, "cang")
    text = observe(w, "cang")
    assert "雨" not in text and "节庆" not in text and "夜" not in text
