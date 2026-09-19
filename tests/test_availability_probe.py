# -*- coding: utf-8 -*-
"""决策层可得性探针（NPC_AVAILABILITY，2026-09-19）。

**解决什么**：`resource_site()` 此前只在**规划层**（`_plan_steps`）和审查层用，
决策/抽签阶段不看可得性 → NPC 会先抽中"采木材"，到规划才发现采不到，
于是走"尝试 → 失败 → 冷却 5 tick → 冷了再试"的周期，并在记忆里堆"没找到地方"的假失败。

**开关语义（家规：默认关 = 与旧版逐字节一致）**
- 关（默认）：候选**不过滤**，采不到仍由规划层兜底（既有行为，两条老用例钉着它）
- 开：组装候选时跳过"当前无地可采"的 gather 项

**口径一致性**：`_item_available` 与 `_plan_steps` 的 gather 分支用**同一个**
`resource_site`、**同一处**位置来源（`world["actors"][id]["position"]`），
所以"决策层放行的活，规划层一定能编排"。
"""
import random

import pytest

from npc.npc import NPC
from npc.scheduler import (_item_available, availability_enabled,
                           _choose_routine_item, resource_site)


@pytest.fixture
def probe_on(monkeypatch):
    monkeypatch.setenv("NPC_AVAILABILITY", "1")


@pytest.fixture
def probe_off(monkeypatch):
    monkeypatch.delenv("NPC_AVAILABILITY", raising=False)


def _npc(pid, routine, world):
    persona = {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [],
        "rules": {"replies": {"好": "好的。"}, "fallback": "嗯。"},
        "routine": routine,
    }
    return NPC(persona=persona, world=world, store_dir="npc/store_test")


def _world_with_no_wood(pid):
    """参考世界，但把**所有**地点的木材清零 → 天下无木可采。"""
    from npc.world import default_world
    w = default_world()
    for loc in w["locations"].values():
        if "木材" in loc.get("resources", {}):
            loc["resources"]["木材"] = 0
    return w


def _pick(npc, world, seed=0):
    return _choose_routine_item(npc, world, random.Random(seed))


# ── 开关默认态 ──────────────────────────────────────────────

def test_flag_defaults_off(probe_off):
    """家规：默认关（关 = 与旧版逐字节一致）。"""
    assert availability_enabled() is False


def test_flag_reads_env(probe_on):
    assert availability_enabled() is True


# ── 关：与旧版一致（候选不过滤）──────────────────────────────

def test_off_still_picks_infeasible_gather(probe_off):
    """**关** = 旧行为：采不到也照排在候选里（由规划层兜底 + 记失败 + 冷却）。"""
    w = _world_with_no_wood("offa")
    npc = _npc("offa", [{"action": "gather", "resource": "木材", "count": 1, "weight": 1}], w)
    assert _pick(npc, w) is not None          # 仍会被选中
    assert resource_site(w, npc.actor_pos, "木材") is None   # 但确实采不到


# ── 开：跳过不可行 ─────────────────────────────────────────

def test_on_skips_infeasible_gather(probe_on):
    """**开**：唯一候选不可行 → 候选空 → 返回 None（NPC 静止，而不是白撞一次）。"""
    w = _world_with_no_wood("ona")
    npc = _npc("ona", [{"action": "gather", "resource": "木材", "count": 1, "weight": 1}], w)
    assert _pick(npc, w) is None


def test_on_falls_through_to_other_item(probe_on):
    """**开**：不可行的 gather 被跳过 → 选别的活（这就是"自己想下一步"）。"""
    w = _world_with_no_wood("onb")
    npc = _npc("onb", [
        {"action": "gather", "resource": "木材", "count": 1, "weight": 1},
        {"action": "rest", "ticks": 1, "weight": 1},
    ], w)
    picked = _pick(npc, w)
    assert picked is not None and picked["action"] == "rest"


def test_on_keeps_reachable_gather(probe_on):
    """**开**：资源可得时照旧可选（探针只挡不可行的，不误伤）。"""
    from npc.world import default_world
    w = default_world()
    npc = _npc("onc", [{"action": "gather", "resource": "木材", "count": 1, "weight": 1}], w)
    picked = _pick(npc, w)
    assert picked is not None and picked["action"] == "gather"


def test_on_skips_unreachable_resource(probe_on):
    """探针同时覆盖"够不着"：资源有量但无路径 → 判为不可行。"""
    from npc.world import default_world
    w = default_world()
    w["locations"]["孤岛"] = {"desc": "四面环水", "resources": {"陨铁": 5}, "exits": ["孤岛"]}
    npc = _npc("ond", [{"action": "gather", "resource": "陨铁", "count": 1, "weight": 1}], w)
    assert resource_site(w, npc.actor_pos, "陨铁") is None   # 确实够不着
    assert _pick(npc, w) is None                             # 于是被跳过


# ── 白名单：只挡 gather，别误伤别的动作 ──────────────────────

@pytest.mark.parametrize("item", [
    {"action": "rest", "ticks": 1},                       # 无资源概念
    {"action": "say"},                                    # 无资源概念
    {"action": "craft", "recipe": "木石工具"},             # 材料由规划层查
    {"action": "gather"},                                 # 缺 resource → 交规划层报错
    {"action": "gather", "resource": ""},                 # 空 resource → 同上
    {"action": "自定义动作", "resource": "木材"},           # 不认识的动作
])
def test_item_available_only_judges_gather(probe_on, item):
    from npc.world import default_world
    w = default_world()
    npc = _npc("one", [item], w)
    assert _item_available(npc, w, item) is True


# ── 口径一致性（本探针存在的意义）──────────────────────────

def test_decision_layer_verdict_matches_planning_layer(probe_on):
    """**核心锚**：决策层说可行 ⇒ 规划层一定能编排（两层口径必须同一套）。

    做法：造一批"资源/可达性各异"的世界，逐项对比
    `_item_available`（决策层）与 `_plan_steps is not None`（规划层）。
    只比 gather 项 —— 其他动作的不可行原因不归本探针管。
    """
    from npc.scheduler import _plan_steps
    from npc.world import default_world

    cases = []
    # ① 正常可得
    cases.append((default_world(), {"action": "gather", "resource": "木材", "count": 1}, "村庄"))
    # ② 全世界采空
    cases.append((_world_with_no_wood("x"), {"action": "gather", "resource": "木材", "count": 1}, "村庄"))
    # ③ 够不着
    w3 = default_world()
    w3["locations"]["孤岛"] = {"desc": "x", "resources": {"陨铁": 5}, "exits": ["孤岛"]}
    cases.append((w3, {"action": "gather", "resource": "陨铁", "count": 1}, "村庄"))
    # ④ 资源名世界根本没有
    cases.append((default_world(), {"action": "gather", "resource": "不存在的东西", "count": 1}, "村庄"))

    for i, (w, item, pos) in enumerate(cases):
        npc = _npc(f"cmp{i}", [item], w)
        w["actors"][f"cmp{i}"]["position"] = pos
        available = _item_available(npc, w, item)
        plannable = _plan_steps(npc, w, item) is not None
        assert available == plannable, (
            f"case{i}: 决策层 available={available} 但规划层 plannable={plannable} —— 两层口径打架")
