"""G2 目标真值层回归锚（2026-09-16）：生命周期 / 前置 / 截止 / 由后果推进 / 种子兼容。

《NPC大脑架构》§29.3 G2 / §30.1 第 4 步第二根柱子。
本文件钉四件事：
  ① 人设 `goals` 只是**种子**（兼容现状 `{文本:{progress,target}}`，坏数据不炸）；
  ② 状态机：PENDING/ACTIVE/BLOCKED/COMPLETED/FAILED/ABANDONED 及各转换；
  ③ 进度推进的**唯一来源是代码**：`apply_result` 只认 G3 的 success，失败不推进、不判死；
  ④ 序列化往返一致（将来进记忆卡用）。
"""
import json

from npc.action_result import ActionResult
from npc.goal import (
    ABANDONED, ACTIVE, BLOCKED, COMPLETED, FAILED, PENDING,
    Goal, GoalQueue, goal_id,
)


def _ok(action="gather", resource="木材"):
    return ActionResult(action=action, who="cang", success=True, message="ok",
                        world_changes={"inventory": {resource: 1}}, observation="x")


def _fail(action="gather"):
    return ActionResult(action=action, who="cang", success=False, message="采尽了",
                        error_code="resource_depleted")


class TestSeedFromPersona:
    """人设 goals → 初始种子（现状形态兼容 + 制作者友好）。"""

    def test_seed_reads_current_shape(self):
        persona = {"goals": {"雪来之前再搭两个棚子": {"progress": 0, "target": 2},
                             "部落安稳过冬": {"progress": 3, "target": 3}}}
        q = GoalQueue.seed_from_persona(persona, world_tick=7)
        assert len(q) == 2
        building = q.get(goal_id("雪来之前再搭两个棚子"))
        assert building.status == ACTIVE and building.target == 2 and building.progress == 0
        assert building.source == "persona_seed" and building.created_tick == 7
        # 已经满进度的目标直接落 COMPLETED（不复活成"待办"）
        winter = q.get(goal_id("部落安稳过冬"))
        assert winter.status == COMPLETED

    def test_seed_missing_or_empty(self):
        assert len(GoalQueue.seed_from_persona(None)) == 0
        assert len(GoalQueue.seed_from_persona({})) == 0
        assert len(GoalQueue.seed_from_persona({"goals": {}})) == 0   # 默认人设就是这个形态

    def test_seed_skips_bad_entries(self):
        """坏数据跳过不炸（与 persona_loader 同款制作者友好）。"""
        assert len(GoalQueue.seed_from_persona({"goals": "每天砍柴"})) == 0   # 不是 dict
        persona = {"goals": {"": {"target": 2}, "三根": {"target": "三根"}, "正经目标": "not-a-dict"}}
        q = GoalQueue.seed_from_persona(persona)
        assert sorted(g.text for g in q.all()) == ["三根", "正经目标"], "空文本要丢"
        assert all(g.target == 1 for g in q.all()), "target 非法退回 1；spec 非 dict 当空"

    def test_seed_is_idempotent(self):
        """重复 seed 不翻倍（同文本同 id，覆盖）。"""
        persona = {"goals": {"目标A": {"target": 2}}}
        q = GoalQueue.seed_from_persona(persona)
        q.add(Goal(text="目标A", target=2))
        assert len(q) == 1


class TestLifecycle:
    """状态机：推进 / 完成 / 失败 / 放弃 / 前置 / 截止。"""

    def test_advance_until_complete(self):
        q = GoalQueue()
        q.add(Goal(text="搭棚子", target=2))
        gid = goal_id("搭棚子")
        assert q.advance(gid).progress == 1 and q.get(gid).status == ACTIVE
        assert q.advance(gid).progress == 2 and q.get(gid).status == COMPLETED
        assert q.active() == []          # 完成后不再出现在可推进列表

    def test_completed_goal_never_resurrects(self):
        q = GoalQueue()
        q.add(Goal(text="搭棚子", target=1))
        gid = goal_id("搭棚子")
        q.advance(gid)
        before = q.get(gid).progress
        q.advance(gid, 5)
        assert q.get(gid).status == COMPLETED and q.get(gid).progress == before
        assert q.abandon(gid).status == COMPLETED   # 终结态不可被改

    def test_prerequisites_block_then_unblock(self):
        q = GoalQueue()
        first = q.add(Goal(text="攒木材", target=1))
        second = q.add(Goal(text="搭棚子", target=1, prerequisites=[first.id]))
        assert second.status == PENDING, "有前置 → 先不激活"
        assert q.refresh()["unblocked"] == []
        assert second.status == BLOCKED
        q.advance(first.id)
        out = q.refresh()
        assert second.id in out["unblocked"] and second.status == ACTIVE

    def test_deadline_expires_but_not_terminal(self):
        q = GoalQueue()
        done = q.add(Goal(text="已完成的", target=1, deadline_tick=5))
        q.advance(done.id)
        live = q.add(Goal(text="要过期的", target=1, deadline_tick=10))
        out = q.refresh(world_tick=11)
        assert out["expired"] == [live.id] and live.status == FAILED
        assert done.status == COMPLETED, "已完成的不会被超期改判"

    def test_abandon_and_fail(self):
        q = GoalQueue()
        g = q.add(Goal(text="算了", target=1))
        assert q.abandon(g.id).status == ABANDONED
        g2 = q.add(Goal(text="做不成", target=1))
        assert q.fail(g2.id).status == FAILED

    def test_active_sorted_by_priority(self):
        q = GoalQueue()
        q.add(Goal(text="小事", target=1, priority=1))
        q.add(Goal(text="大事", target=1, priority=9))
        assert q.active()[0].text == "大事"


class TestAdvancedByActionResult:
    """进度只能由代码按**真实后果**推进（G3 的第一个消费点）。"""

    def test_success_advances(self):
        q = GoalQueue()
        q.add(Goal(text="砍两根木材", target=2, action="gather", params={"resource": "木材"}))
        gid = goal_id("砍两根木材")
        q.apply_result(gid, _ok())
        assert q.get(gid).progress == 1
        q.apply_result(gid, _ok())
        assert q.get(gid).status == COMPLETED

    def test_failure_neither_advances_nor_kills(self):
        q = GoalQueue()
        q.add(Goal(text="砍两根木材", target=2))
        gid = goal_id("砍两根木材")
        q.apply_result(gid, _fail())
        g = q.get(gid)
        assert g.progress == 0 and g.status == ACTIVE, "失败只说明这一步没成，不等于目标作废"

    def test_actions_for_matches_active_goals(self):
        """G1 的接口：按 (action, resource) 找到该加权的活动目标。"""
        q = GoalQueue()
        q.add(Goal(text="砍木材", target=1, action="gather", params={"resource": "木材"}, priority=7))
        q.add(Goal(text="捡石头", target=1, action="gather", params={"resource": "石头"}))
        hit = q.actions_for(("gather", "木材"))
        assert len(hit) == 1 and hit[0].text == "砍木材" and hit[0].priority == 7
        assert q.actions_for(("gather", "浆果")) == []


class TestSerialisation:
    """往返一致（将来进记忆卡/落盘用）。"""

    def test_roundtrip(self):
        q = GoalQueue()
        q.add(Goal(text="A", target=2, priority=8, action="gather", params={"resource": "木材"},
                   deadline_tick=99, prerequisites=[goal_id("B")]))
        q.add(Goal(text="B", target=1))
        # A 有前置(B) → 不激活；B 无前置 → ACTIVE
        assert q.get(goal_id("A")).status == PENDING and q.get(goal_id("B")).status == ACTIVE
        dumped = json.loads(json.dumps(q.to_list(), ensure_ascii=False))
        again = GoalQueue.from_list(dumped)
        assert again.to_list() == q.to_list(), "往返必须逐字段一致"
        assert again.get(goal_id("A")).prerequisites == [goal_id("B")]
        assert again.active()[0].text == "B"

    def test_from_list_skips_junk(self):
        q = GoalQueue.from_list([{"text": "好的", "target": 1}, {"no_text": 1}, "字符串", None])
        assert [g.text for g in q.all()] == ["好的"]
