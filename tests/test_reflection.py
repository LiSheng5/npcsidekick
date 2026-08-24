"""反思归纳（阶段① 海马体升级）测试 — 触发阈值 / 规则兜底 / 不重复 / 持久化 / LLM。"""
import pytest

from npc.npc import NPC, _reflect_rules


class TestReflectRulesFallback:
    """无 LLM 时规则兜底: 只复述事实，不编造。"""

    def test_restates_facts_only(self):
        entries = [
            {"content": "帮主角采了 2 根木材", "importance": 8},
            {"content": "帮主角修了屋顶", "importance": 6},
        ]
        text = _reflect_rules(entries)
        assert "帮主角采了 2 根木材" in text
        assert "帮主角修了屋顶" in text
        assert "好人" not in text   # 不发明原文没有的事实


class TestMaybeReflect:
    @pytest.fixture
    def npc(self):
        n = NPC(store_dir="npc/store_test")
        n.use_llm = False     # 走规则兜底，测试确定性
        return n

    def test_below_threshold_no_reflection(self, npc):
        npc.remember("一件小事", importance=3)
        assert npc.maybe_reflect() is None
        assert len([e for e in npc.memory.all() if e.get("category") == "reflection"]) == 0

    def test_reflect_when_threshold_met(self, npc):
        npc.remember("帮主角采了 2 根木材", importance=6)
        npc.remember("帮主角修了屋顶", importance=7)   # 13 >= 12
        text = npc.maybe_reflect()
        assert text is not None
        reflections = [e for e in npc.memory.all() if e.get("category") == "reflection"]
        assert len(reflections) == 1
        assert reflections[0]["importance"] == 8
        assert "帮主角" in text

    def test_no_re_reflect_same_batch(self, npc):
        npc.remember("帮主角采了 2 根木材", importance=6)
        npc.remember("帮主角修了屋顶", importance=7)
        npc.maybe_reflect()
        npc.remember("又帮主角捡了柴", importance=3)   # 新条目权重低，不够阈值
        assert npc.maybe_reflect() is None             # 不重复反思同一批

    def test_reflects_new_batch_later(self, npc):
        npc.remember("帮主角采了 2 根木材", importance=6)
        npc.remember("帮主角修了屋顶", importance=7)
        npc.maybe_reflect()
        npc.remember("陪主角去河边", importance=6)
        npc.remember("给主角摘了浆果", importance=6)   # 新一批 12 >= 12
        assert npc.maybe_reflect() is not None
        assert len([e for e in npc.memory.all() if e.get("category") == "reflection"]) == 2

    def test_reflected_upto_persists(self, npc):
        npc.remember("帮主角采了 2 根木材", importance=6)
        npc.remember("帮主角修了屋顶", importance=7)
        npc.maybe_reflect()
        npc.save()
        restored = NPC.load("cang", store_dir="npc/store_test")
        restored.use_llm = False
        assert restored._reflected_upto == npc._reflected_upto
        assert restored.maybe_reflect() is None   # 恢复后不重复反思已归纳批次


class TestReflectWithLLM:
    """LLM 路径: 归纳出高层结论（Mock LLM）。"""

    class _FakeLLM:
        def chat(self, messages, **kwargs):
            return type("R", (), {"content": "苍靠得住，总帮主角办事。"})()

    def test_llm_reflection_stored(self):
        npc = NPC(store_dir="npc/store_test")
        npc.use_llm = True
        npc._llm = self._FakeLLM()
        npc.remember("帮主角采了 2 根木材", importance=6)
        npc.remember("帮主角修了屋顶", importance=7)
        text = npc.maybe_reflect()
        assert text == "苍靠得住，总帮主角办事。"
        reflections = [e for e in npc.memory.all() if e.get("category") == "reflection"]
        assert reflections[0]["content"] == "苍靠得住，总帮主角办事。"
