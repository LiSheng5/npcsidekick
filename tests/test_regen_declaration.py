# -*- coding: utf-8 -*-
"""资源回补声明化（2026-09-18）—— 引擎层不持有"资源会再生"这个游戏经济设定。

世界用 `_resource_regen` 声明每 tick 回补多少；未声明 = 不回补（会真的采空）。
参考世界声明 1，维持旧行为（零回归锚）。
"""
import random

from npc.scheduler import tick_round
from npc.world import default_world


def _tick(w):
    """推一帧（无 NPC 参与，只看世界层面的回补）。"""
    return tick_round(w, {}, rng=random.Random(0))


def test_undeclared_world_no_regen():
    """未声明 `_resource_regen` → 采空后 tick 资源不变（真的会采空）。"""
    w = default_world()
    w.pop("_resource_regen")
    w["locations"]["森林"]["resources"]["木材"] = 0
    _tick(w)
    assert w["locations"]["森林"]["resources"]["木材"] == 0


def test_declared_rate_regen():
    """`_resource_regen=2` → 一次 tick 回补 2（速率由声明说了算）。"""
    w = default_world()
    w["_resource_regen"] = 2
    w["locations"]["森林"]["resources"]["木材"] = 0
    _tick(w)
    assert w["locations"]["森林"]["resources"]["木材"] == 2


def test_default_world_regen_unchanged():
    """参考世界仍声明 1 → 行为与旧版逐字节一致（零回归锚）。"""
    w = default_world()
    assert w["_resource_regen"] == 1
    w["locations"]["森林"]["resources"]["木材"] = 0
    _tick(w)
    assert w["locations"]["森林"]["resources"]["木材"] == 1


def test_regen_capped_by_resource_caps():
    """回补不越过 `_resource_caps` 上限。"""
    w = default_world()
    w["_resource_regen"] = 5
    w["_resource_caps"] = {"木材": 3}
    w["locations"]["森林"]["resources"]["木材"] = 1
    _tick(w)
    assert w["locations"]["森林"]["resources"]["木材"] == 3


def test_bool_rate_treated_as_zero():
    """`_resource_regen: true` 不得被当成 1（bool 是 int 子类，须显式挡掉）。"""
    w = default_world()
    w["_resource_regen"] = True
    w["locations"]["森林"]["resources"]["木材"] = 0
    _tick(w)
    assert w["locations"]["森林"]["resources"]["木材"] == 0


def test_negative_rate_treated_as_zero():
    """负数速率 → 0（世界不至于被声明成负增长）。"""
    w = default_world()
    w["_resource_regen"] = -3
    w["locations"]["森林"]["resources"]["木材"] = 0
    _tick(w)
    assert w["locations"]["森林"]["resources"]["木材"] == 0
