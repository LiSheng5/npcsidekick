"""三角色审查管线测试 — 落账口 + 按场景审查 + 端到端焊死。"""
import pytest

from npc.npc import NPC
from npc.reviewer import (compile_task, review_dialogue, review_task,
                          should_review, approve_action, set_approval)


class TestShouldReview:
    """按场景触发审查（成本闸门）。"""

    def test_command_always_reviewed(self):
        assert should_review("command") is True

    def test_dialogue_plain_not_reviewed(self):
        assert should_review("dialogue", reply="今天天气不错") is False

    def test_dialogue_promise_reviewed(self):
        assert should_review("dialogue", reply="我这就去给你采浆果") is True

    def test_dialogue_risky_reviewed(self):
        assert should_review("dialogue", player_input="那人偷了东西") is True

    def test_unknown_scene_not_reviewed(self):
        assert should_review("whatever") is False


class TestCompileTask:
    """B2 编译: 玩家诉求 → 任务单。"""

    def test_compiles_count(self):
        assert compile_task("给我两根木材") == {
            "action": "gather", "resource": "木材", "count": 2}

    def test_compiles_alias(self):
        assert compile_task("去帮我砍点柴")["resource"] == "木材"

    def test_compiles_digit_count(self):
        assert compile_task("给我3个浆果")["count"] == 3

    def test_no_task_without_resource(self):
        assert compile_task("我要去散步") is None

    def test_no_task_without_intent(self):
        assert compile_task("木材在哪") is None


class TestReviewTask:
    """A 审查任务可行性（规则）。"""

    @pytest.fixture
    def npc(self):
        return NPC(store_dir="npc/store_test")

    def test_feasible(self, npc):
        ok, reason = review_task(npc, {"action": "gather", "resource": "木材", "count": 1})
        assert ok and reason == ""

    def test_exhausted_rejected(self, npc):
        for loc in npc.world["locations"].values():
            for res in loc.get("resources", {}):
                loc["resources"][res] = 0
        ok, reason = review_task(npc, {"action": "gather", "resource": "木材", "count": 1})
        assert not ok and "弄不到" in reason


class TestReviewDialogue:
    """A 审查对话（规则层）: 禁忌 + 承诺必须落账。"""

    @pytest.fixture
    def npc(self):
        return NPC(store_dir="npc/store_test")

    def test_taboo_blocked(self, npc):
        npc.persona["taboos"] = ["追跑得快的猎物"]
        ok, reason = review_dialogue(npc, "我去追跑得快的猎物", "你好")
        assert not ok and "禁忌" in reason

    def test_promise_without_booking_blocked(self, npc):
        # LLM 答应去办事但 pending_task 未落账 → 拦截（口是心非防线）
        ok, reason = review_dialogue(npc, "我这就去给你采浆果", "聊聊天")
        assert not ok and "未落账" in reason

    def test_plain_reply_passes(self, npc):
        ok, reason = review_dialogue(npc, "嗯，火塘边坐着说。", "吃了吗")
        assert ok


class TestTalkReviewWeld:
    """端到端焊死: LLM 答应没落账 → 审查拦截重生成（不再口头承诺）。"""

    class _PromisingLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return type("R", (), {"content": "好，我这就去给你采浆果。"})()
            return type("R", (), {"content": "嗯，这个季节浆果不好说，回头再聊。"})()

    def test_promise_forced_off(self):
        fake = self._PromisingLLM()
        npc = NPC(store_dir="npc/store_test")
        npc.use_llm = True
        npc._llm = fake
        reply = npc.talk("聊聊天")
        assert fake.calls == 2            # 原始 + 1 次重生成
        assert npc.pending_task is None   # 没落账
        assert "我这就去" not in reply     # 承诺被逼退

class TestApproveAction:
    """Approval 三态（Codex /approvals 对照）: allow/ask/deny + 运行时覆盖。"""

    def test_defaults_by_action(self):
        assert approve_action("gather") == "allow"
        assert approve_action("craft") == "ask"
        assert approve_action("move") == "ask"
        assert approve_action("deliver") == "ask"

    def test_unknown_action_asks(self):
        assert approve_action("teleport") == "ask"

    def test_loose_policies_allow_all(self):
        for pol in ("on-failure", "never"):
            assert approve_action("deliver", pol) == "allow"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("NPC_APPROVAL_DELIVER", "deny")
        assert approve_action("deliver") == "deny"
        monkeypatch.setenv("NPC_APPROVAL_GATHER", "ask")
        assert approve_action("gather") == "ask"

    def test_runtime_set_approval(self):
        assert set_approval("deliver", "deny") is True
        assert approve_action("deliver") == "deny"
        set_approval("deliver", "ask")   # 还原, 避免污染其他测试
        assert set_approval("nope", "allow") is False
        assert set_approval("deliver", "bogus") is False

    def test_npc_overrides_take_priority(self):
        """实例覆盖优先于全局覆盖: 每个 NPC 可独立配置。"""
        overrides = {"deliver": "deny"}
        assert approve_action("deliver", overrides=overrides) == "deny"
        assert approve_action("gather", overrides=overrides) == "allow"  # 未覆盖走默认
        set_approval("deliver", "allow")                                # 全局设 allow
        assert approve_action("deliver", overrides=overrides) == "deny"  # 实例仍优先
        set_approval("deliver", "ask")                                  # 还原全局
        assert set_approval("deliver", "deny", overrides) is True
        assert approve_action("deliver", overrides=overrides) == "deny"
        set_approval("deliver", "ask", overrides)                        # 还原实例


class TestTaskCommandDeny:
    """deny 动作: 不编译落账, 直接拒绝。"""

    def test_denied_action_not_booked(self, monkeypatch):
        npc = NPC(store_dir="npc/store_test")
        npc.use_llm = False
        monkeypatch.setenv("NPC_APPROVAL_GATHER", "deny")
        reply = npc.talk("给我两根木材")
        assert npc.pending_task is None
        assert "不能" in reply



# ── 任务书#02: 任务回路词表(B2 编译) + 清单加载校验 ──────────

class TestCompileMoveTask:
    """词表兜底: 跟我走/跟着我 → follow_player; 陪我去X/到X去 → goto。

    只在含新动作的清单下生效(默认清单零变化——向后兼容)。
    """

    @pytest.fixture(autouse=True)
    def _gta_manifest(self):
        from npc.reviewer import load_manifest

        load_manifest({
            "follow_player": {"tier": 3, "approval": "ask", "params": []},
            "goto": {"tier": 2, "approval": "ask", "params": ["地点"]},
        })
        yield
        load_manifest(None)   # 恢复默认, 防污染其他用例

    def test_follow_phrase_walk_with_me(self):
        assert compile_task("跟我走") == {"action": "follow_player"}

    def test_follow_phrase_follow_me(self):
        assert compile_task("跟着我") == {"action": "follow_player"}

    def test_goto_lead_phrase(self):
        assert compile_task("陪我去河边") == {"action": "goto", "地点": "河边"}

    def test_goto_weave_phrase(self):
        assert compile_task("到河边去") == {"action": "goto", "地点": "河边"}

    def test_default_manifest_keeps_old_behavior(self):
        """默认清单(无 follow/goto) → 这些话不接单(向后兼容)。"""
        from npc.reviewer import load_manifest

        load_manifest(None)
        assert compile_task("跟我走") is None
        assert compile_task("陪我去河边") is None
        # 采集类不受影响
        assert compile_task("给我两根木材")["action"] == "gather"


class TestLoadManifestValidation:
    """任务书#02 验收 1: 清单新增动作 + 非法清单不半套生效。"""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from npc.reviewer import load_manifest

        yield
        load_manifest(None)

    def test_loads_follow_and_goto(self):
        from npc.reviewer import get_manifest, load_manifest

        load_manifest({
            "follow_player": {"tier": 3, "approval": "ask", "params": []},
            "goto": {"tier": 2, "approval": "ask", "params": ["地点"]},
        })
        m = get_manifest()
        assert m["follow_player"] == {"tier": 3, "approval": "ask",
                                      "params": [], "desc": ""}
        assert m["goto"]["tier"] == 2 and m["goto"]["params"] == ["地点"]

    def test_invalid_tier_keeps_old_manifest(self):
        from npc.reviewer import get_manifest, load_manifest

        load_manifest(None)
        before = get_manifest()
        with pytest.raises(ValueError):
            load_manifest({"bad": {"tier": 9, "approval": "ask", "params": []}})
        assert get_manifest() == before   # 原清单保持不变(不会半套生效)
