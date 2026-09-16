"""G3 `ActionResult` 回归锚（2026-09-16）：结构化契约 + **零行为变化**。

《NPC大脑架构》§29.3 G3 / §30.1 第 4 步第一根柱子。
本文件钉三件事：
  ① 后果字段客观（success / error_code / world_changes / duration_ms / observation）；
  ② 失败时**世界不被改动**（差分必须为空）；
  ③ **旧契约不受影响**：`apply_action` 仍是三件套，包装前后世界状态逐字节一致。
"""
import json
from copy import deepcopy

from npc.action_result import ActionResult, apply_action_structured, classify_error
from npc.world import apply_action, default_world


def _world(who="cang", pos="村庄", inv=None, delivered=None):
    w = default_world()
    w["actors"] = {who: {"position": pos, "inventory": dict(inv or {})}}
    w["protagonist"] = {"position": "村庄", "name": "主角"}
    w["delivered"] = dict(delivered or {})
    w["_tick"] = 0
    return w


class TestStructuredResult:
    """结构化字段：说清"发生了什么、成不成、为什么不成"。"""

    def test_gather_success_reports_delta(self):
        w = _world(pos="森林")   # 村庄不产木材（默认世界：森林产木材 / 河边产浆果 / 矿洞产石头）
        w2, r = apply_action_structured(w, "gather", {"resource": "木材"}, who="cang")
        assert isinstance(r, ActionResult) and r.success is True
        assert r.error_code is None
        assert r.world_changes["inventory"] == {"木材": 1}
        assert "背包 木材+1" in r.observation
        assert r.duration_ms >= 0.0
        assert r.message and r.action == "gather" and r.who == "cang"

    def test_move_success_reports_position_change(self):
        _, r = apply_action_structured(_world(), "move", {"dest": "森林"}, who="cang")
        assert r.success and r.world_changes["position"] == {"from": "村庄", "to": "森林"}
        assert "位置 村庄→森林" in r.observation

    def test_deliver_success_reports_delivered_delta(self):
        w = _world(inv={"木材": 1})
        _, r = apply_action_structured(w, "deliver", {"resource": "木材"}, who="cang")
        assert r.success
        assert r.world_changes["inventory"] == {"木材": -1}
        assert r.world_changes["delivered"] == {"木材": 1}
        assert "累计交付 木材+1" in r.observation

    def test_say_has_no_state_delta(self):
        w = _world()
        _, r = apply_action_structured(w, "say", {"text": "嗯。"}, who="cang")
        assert r.success and r.observation.endswith("已执行")
        assert "inventory" not in r.world_changes and "position" not in r.world_changes


class TestFailureCodes:
    """失败：给出稳定 error_code，且**世界一动不动**。"""

    def test_depleted_resource(self):
        w = _world(pos="森林")
        w["locations"]["森林"]["resources"]["木材"] = 0
        w2, r = apply_action_structured(w, "gather", {"resource": "木材"}, who="cang")
        assert r.success is False and r.error_code == "resource_depleted"
        assert r.world_changes == {}, "失败不该产生差分"
        # 与"不包装"跑一遍逐字节对齐（真正的零行为变化锚；`actor_of` 惰性补 stamina 属既有行为）
        w3 = _world(pos="森林")
        w3["locations"]["森林"]["resources"]["木材"] = 0
        plain3, _, _ = apply_action(w3, "gather", {"resource": "木材"}, who="cang")
        assert json.dumps(w2, ensure_ascii=False, sort_keys=True) == \
            json.dumps(plain3, ensure_ascii=False, sort_keys=True)

    def test_unknown_action(self):
        _, r = apply_action_structured(_world(), "fly", {}, who="cang")
        assert r.success is False and r.error_code == "unknown_action"

    def test_not_carried(self):
        _, r = apply_action_structured(_world(), "deliver", {"resource": "木材"}, who="cang")
        assert r.error_code == "not_carried"

    def test_unreachable(self):
        _, r = apply_action_structured(_world(), "move", {"dest": "不存在"}, who="cang")
        assert r.error_code == "unreachable"

    def test_protagonist_absent(self):
        w = _world(inv={"木材": 1})
        w["protagonist"]["position"] = "森林"
        _, r = apply_action_structured(w, "deliver", {"resource": "木材"}, who="cang")
        assert r.error_code == "protagonist_absent"

    def test_unmatched_message_returns_none(self):
        """匹配不到就留空 —— 不猜（诚实边界）。"""
        assert classify_error("某种从没见过的失败") is None


class TestZeroBehaviourChange:
    """包装层不许改变行为：同一行动，包与不包的世界状态必须逐字节一致。"""

    def test_world_state_identical_with_and_without_wrapper(self):
        for action, params in (("gather", {"resource": "木材"}), ("move", {"dest": "森林"}),
                               ("say", {"text": "嗯。"}), ("deliver", {"resource": "木材"}),
                               ("gather", {"resource": "不存在的东西"})):
            plain, ok, msg = apply_action(deepcopy(_world(inv={"木材": 1})), action, params, who="cang")
            wrapped, r = apply_action_structured(_world(inv={"木材": 1}), action, params, who="cang")
            assert r.success is ok and r.message == msg, f"{action}: 成败/消息必须一致"
            assert json.dumps(wrapped, ensure_ascii=False, sort_keys=True) == \
                json.dumps(plain, ensure_ascii=False, sort_keys=True), f"{action}: 世界状态必须一致"

    def test_legacy_signature_untouched(self):
        """`apply_action` 仍是 (world, ok, msg) 三件套（调用方零改动）。"""
        ret = apply_action(_world(), "gather", {"resource": "木材"}, who="cang")
        assert isinstance(ret, tuple) and len(ret) == 3
        assert isinstance(ret[1], bool) and isinstance(ret[2], str)
