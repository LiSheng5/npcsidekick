"""G1 另一半：反思 lesson 进决策（2026-09-16）——「反思改变行为」的回归锚。

《NPC大脑架构》§30.1 第 5 步 / 审计 TD1。钉五件事：
  ① 反思条目的可选字段 `scope`/`recommendation` 归一（**两者齐备且合法才写**，否则完全不写键 → 老条目零回归）；
  ② 作用域匹配 = `item.get(k) == v` 全中（引擎不预设语义）；
  ③ 权重乘子 = `LESSON_WEIGHT_BASE ** rec`（+1→×2 / 0→×1 / −1→×0.5），多命中取绝对值最大；
  ④ 开关 `NPC_LESSONS` 关 = 逐字节同旧版（乘子恒 1、条目压根不参与）；
  ⑤ **A/B 对比**：同一任务，有 lesson 与无 lesson 的决策分布必须**可测地不同**（审计点名的验收）。
"""
import random

import pytest

from npc.memory import NPCMemory, clean_recommendation, clean_scope
from npc.memory_card import _parse_typed_reflection
from npc.npc import NPC
from npc.scheduler import _choose_routine_item, _dynamic_weight, _lesson_factor, lessons_enabled
from npc.world import default_world

WOOD = {"action": "gather", "resource": "木材", "count": 1, "weight": 1}
BERRY = {"action": "gather", "resource": "浆果", "count": 1, "weight": 1}


def _world():
    world = default_world()
    world["actors"] = {}
    world["protagonist"] = {"position": "村庄", "name": "主角"}
    world["_tick"] = 0
    return world


def _npc(pid, routine, world, lessons=()):
    """lessons: [(scope, recommendation), ...] → 灌进这个 NPC 的记忆卡（category=reflection）。"""
    persona = {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [], "rules": {"replies": {}, "fallback": "嗯。"},
        "routine": list(routine), "goals": {},
    }
    npc = NPC(persona=persona, world=world, store_dir="npc/store_test")
    for scope, rec in lessons:
        npc.memory.add("从上次失败学的：这类活要谨慎", importance=8,
                       category="reflection", mtype="episodic",
                       scope=scope, recommendation=rec)
    return npc


class TestFieldNormalisation:
    """可选字段归一：合法才留，非法就丢（不猜、不崩）。"""

    def test_clean_scope_accepts_short_string_dict(self):
        assert clean_scope({"action": "gather", "resource": "木材"}) == \
            {"action": "gather", "resource": "木材"}
        assert clean_scope({" action ": " gather "}) == {"action": "gather"}   # 去空白

    @pytest.mark.parametrize("bad", [None, {}, "gather", [], {"action": ""}, {"action": 1},
                                     {"a": "1", "b": "2", "c": "3", "d": "4"}])
    def test_clean_scope_rejects_junk(self, bad):
        assert clean_scope(bad) is None

    @pytest.mark.parametrize("raw,expect", [(1, 1.0), (0.5, 0.5), (-0.5, -0.5),
                                            (5, 1.0), (-9, -1.0)])
    def test_clean_recommendation_clamps(self, raw, expect):
        assert clean_recommendation(raw) == expect

    @pytest.mark.parametrize("bad", [None, "1", True, float("nan")])
    def test_clean_recommendation_rejects_junk(self, bad):
        assert clean_recommendation(bad) is None


class TestWritePath:
    """写入路径：两个字段都合法才落键（否则一个都不写 → 与旧版逐字节一致）。"""

    def test_both_valid_writes_both_keys(self):
        mem = NPCMemory()
        mem.add("教训", category="reflection", scope={"action": "gather"},
                recommendation=-1)
        e = mem.all()[0]
        assert e["scope"] == {"action": "gather"} and e["recommendation"] == -1.0

    def test_only_one_given_writes_neither(self):
        mem = NPCMemory()
        mem.add("只有 scope", category="reflection", scope={"action": "gather"})
        mem.add("只有 rec", category="reflection", recommendation=1)
        mem.add("scope 非法", category="reflection", scope="gather", recommendation=1)
        for e in mem.all():
            assert "scope" not in e and "recommendation" not in e

    def test_legacy_entry_keys_unchanged(self):
        """没给新字段时，条目键集与旧版一致（零回归锚）。"""
        mem = NPCMemory()
        mem.add("普通记忆", importance=5, category="general")
        assert set(mem.all()[0]) == {"id", "content", "importance", "category", "created_at"}


class TestTypedReflectionParser:
    """三分类反思解析器：带上 lesson 字段；字段非法只丢字段，不丢整条。"""

    def test_carries_lesson_fields(self):
        raw = ('[{"mtype":"instruction","content":"别在天黑后进林子","importance":7,'
               '"scope":{"action":"gather","resource":"木材"},"recommendation":-1}]')
        out = _parse_typed_reflection(raw)
        assert out[0]["scope"] == {"action": "gather", "resource": "木材"}
        assert out[0]["recommendation"] == -1.0

    def test_bad_lesson_fields_keep_entry(self):
        raw = ('[{"mtype":"episodic","content":"照旧","importance":5,'
               '"scope":"gather","recommendation":"很多"}]')
        out = _parse_typed_reflection(raw)
        assert len(out) == 1 and "scope" not in out[0] and "recommendation" not in out[0]

    def test_legacy_output_has_no_new_keys(self):
        raw = '[{"mtype":"episodic","content":"旧格式","importance":5}]'
        assert set(_parse_typed_reflection(raw)[0]) == {"mtype", "content", "importance"}


class TestLookup:
    """作用域匹配：全中才算命中；老条目永不参与。"""

    def test_matching_and_partial_scope(self):
        mem = NPCMemory()
        mem.add("a", category="reflection", scope={"action": "gather"}, recommendation=1)
        mem.add("b", category="reflection", scope={"resource": "木材"}, recommendation=-1)
        mem.add("c", category="reflection", scope={"action": "gather", "resource": "浆果"},
                recommendation=1)
        hits = mem.lessons_for(WOOD)
        assert {h["content"] for h in hits} == {"a", "b"}, "浆果那条不该命中木材"

    def test_legacy_and_archived_never_hit(self):
        mem = NPCMemory()
        mem.add("老条目（无 scope）", category="reflection")
        mem.add("被归档", category="archived", scope={"action": "gather"}, recommendation=1)
        assert mem.lessons_for(WOOD) == []

    def test_strongest_first(self):
        mem = NPCMemory()
        mem.add("弱", category="reflection", scope={"action": "gather"}, recommendation=-0.2)
        mem.add("强", category="reflection", scope={"action": "gather"}, recommendation=-1)
        assert mem.lessons_for(WOOD)[0]["content"] == "强"


class TestFactor:
    """乘子：base ** rec，多命中取最强；关着/没命中/出故障 → 1。"""

    def test_factor_follows_recommendation(self, monkeypatch):
        monkeypatch.setenv("NPC_LESSONS", "1")
        world = _world()
        assert _lesson_factor(_npc("p", [], world, [( {"action": "gather"}, 1)]), world, WOOD) \
            == pytest.approx(2.0)
        assert _lesson_factor(_npc("m", [], world, [( {"action": "gather"}, -1)]), world, WOOD) \
            == pytest.approx(0.5)
        assert _lesson_factor(_npc("z", [], world, [( {"action": "gather"}, 0)]), world, WOOD) \
            == pytest.approx(1.0)
        # 只绑木材的 lesson：浆果不该被牵连（scope 具体到什么，就只管到什么）
        assert _lesson_factor(_npc("u", [], world, [({"action": "gather", "resource": "木材"}, -1)]),
                              world, BERRY) == pytest.approx(1.0)

    def test_weight_halved_by_avoid_lesson(self, monkeypatch):
        world = _world()
        monkeypatch.delenv("NPC_LESSONS", raising=False)
        neutral = _npc("n", [], world)
        learned = _npc("l", [], world, [({"action": "gather", "resource": "木材"}, -1)])
        base_off = _dynamic_weight(neutral, world, dict(WOOD), 1)
        assert _dynamic_weight(learned, world, dict(WOOD), 1) == pytest.approx(base_off), \
            "关着时 lesson 一个字都不参与"
        monkeypatch.setenv("NPC_LESSONS", "1")
        assert _dynamic_weight(learned, world, dict(WOOD), 1) == pytest.approx(base_off * 0.5)
        assert _dynamic_weight(learned, world, dict(BERRY), 1) == \
            pytest.approx(_dynamic_weight(neutral, world, dict(BERRY), 1))

    def test_env_read_live(self, monkeypatch):
        monkeypatch.delenv("NPC_LESSONS", raising=False)
        assert lessons_enabled() is False
        monkeypatch.setenv("NPC_LESSONS", "1")
        assert lessons_enabled() is True

    def test_fault_degrades_to_neutral(self, monkeypatch):
        monkeypatch.setenv("NPC_LESSONS", "1")
        world = _world()
        npc = _npc("boom", [], world, [({"action": "gather"}, -1)])

        def _boom(*a, **kw):
            raise RuntimeError("记忆层炸了")

        monkeypatch.setattr(type(npc.memory), "lessons_for", _boom)
        assert _lesson_factor(npc, world, WOOD) == 1.0


class TestABComparison:
    """审计点名的 A/B：同一任务，有 lesson / 无 lesson 的决策分布必须可测地不同。"""

    @staticmethod
    def _wood_picks(npc, world, n=200):
        return sum(1 for s in range(n)
                   if _choose_routine_item(npc, world, random.Random(s))["resource"] == "木材")

    def test_avoid_lesson_shifts_decisions(self, monkeypatch):
        monkeypatch.setenv("NPC_LESSONS", "1")
        world = _world()
        learned = _npc("l", [dict(WOOD), dict(BERRY)], world,
                       [({"action": "gather", "resource": "木材"}, -1)])
        plain = _npc("p", [dict(WOOD), dict(BERRY)], world)

        wood_learned = self._wood_picks(learned, world)
        wood_plain = self._wood_picks(plain, world)
        assert wood_learned < wood_plain, \
            f"有'避着砍木头'的反思 → 选木材的次数必须变少（{wood_learned} vs {wood_plain}）"

    def test_switch_off_means_no_difference(self, monkeypatch):
        """关着时，有 lesson 与没 lesson 的决策分布**逐次相同** —— 零回归的 A/B 对照。"""
        monkeypatch.delenv("NPC_LESSONS", raising=False)
        world = _world()
        learned = _npc("l2", [dict(WOOD), dict(BERRY)], world,
                       [({"action": "gather", "resource": "木材"}, -1)])
        plain = _npc("p2", [dict(WOOD), dict(BERRY)], world)
        seq = lambda npc: [_choose_routine_item(npc, world, random.Random(s))["resource"]
                           for s in range(50)]
        assert seq(learned) == seq(plain)
