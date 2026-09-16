"""P-6 回归锚（2026-09-16）：检索打分加 `goal_relevance` 因子（开关 NPC_GOAL_RELEVANCE）。

《NPC大脑架构》§30.1 第 5 步剩下一半 / 审计 Phase 7。钉四件事：
  ① **关着一字不加**：分数与"没有目标"时逐字相同（同分同序）；
  ② 开着且注入了目标 → **与目标相关的记忆排序上升**（可测的行为差异）；
  ③ 注入的目标文本**不落盘**（进卡就会破坏"关时逐字节一致"的锚）；
  ④ 两个开关都关时**连目标队列都不建**（零开销），任一开着才同步。
"""
import json
import random

import pytest

from npc.memory import NPCMemory, goal_relevance_enabled
from npc.npc import NPC
from npc.scheduler import tick_round
from npc.world import default_world


def _world():
    world = default_world()
    world["actors"] = {}
    world["protagonist"] = {"position": "村庄", "name": "主角"}
    world["_tick"] = 0
    return world


def _mem():
    """两条同等重要 / 同等新鲜的记忆（石头在前、木材在后 → 无因子时保持插入序）。"""
    mem = NPCMemory()
    mem.add("捡了石头", importance=5, category="general")
    mem.add("砍了木材", importance=5, category="general")
    for e in mem.all():
        e["created_at"] = 1_000_000.0        # 对齐新鲜度，隔离出唯一变量
    return mem


def _contents(entries):
    return [e["content"] for e in entries]


class TestSwitchOffIsZeroChange:
    """开关关 / 没注入 → 与旧版同分同序。"""

    def test_off_ignores_injected_terms(self, monkeypatch):
        monkeypatch.delenv("NPC_GOAL_RELEVANCE", raising=False)
        mem = _mem()
        base = _contents(mem.retrieve(""))
        mem.set_goal_terms(["多备木材"])
        assert _contents(mem.retrieve("")) == base == ["捡了石头", "砍了木材"]

    def test_on_without_terms_is_identical(self, monkeypatch):
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        mem = _mem()
        assert _contents(mem.retrieve("")) == ["捡了石头", "砍了木材"]

    def test_env_read_live(self, monkeypatch):
        monkeypatch.delenv("NPC_GOAL_RELEVANCE", raising=False)
        assert goal_relevance_enabled() is False
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        assert goal_relevance_enabled() is True


class TestGoalRelevanceRanksUp:
    """开着且注入目标 → 相关记忆升序（这就是"目标影响记忆召回"）。"""

    def test_goal_relevant_entry_moves_up(self, monkeypatch):
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        mem = _mem()
        assert _contents(mem.retrieve("")) == ["捡了石头", "砍了木材"]
        mem.set_goal_terms(["多备木材"])
        assert _contents(mem.retrieve(""))[0] == "砍了木材", "目标相关的记忆应当排到前面"

    def test_unrelated_goal_does_not_reorder(self, monkeypatch):
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        mem = _mem()
        mem.set_goal_terms(["整理浆果"])
        assert _contents(mem.retrieve("")) == ["捡了石头", "砍了木材"]

    def test_terms_sanitised(self, monkeypatch):
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        mem = _mem()
        mem.set_goal_terms(["  多备木材  ", "", None, 42])
        assert mem._goal_terms == ["多备木材"], "空白/非字符串一律丢弃"
        mem.set_goal_terms("不是列表")
        assert mem._goal_terms == []

    def test_huge_garbage_terms_do_not_crash(self, monkeypatch):
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        mem = _mem()
        mem.set_goal_terms(["木" * 20000])
        assert len(mem.retrieve("")) == 2


class TestNotPersisted:
    """注入的目标文本是运行时状态，**不进卡**（否则破坏"关时逐字节一致"）。"""

    def test_to_dict_has_no_goal_terms(self, monkeypatch):
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        mem = _mem()
        before = mem.to_dict()
        mem.set_goal_terms(["多备木材"])
        assert mem.to_dict() == before

    def test_card_payload_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        world = _world()
        npc = NPC(persona={"id": "np", "name": "np", "identity": "t", "personality": "t",
                           "speech_style": "t", "taboos": [],
                           "rules": {"replies": {}, "fallback": "嗯。"},
                           "routine": [], "goals": {}},
                  world=world, store_dir=str(tmp_path))
        npc.save()
        card_before = json.loads((tmp_path / "np_memory.json").read_text(encoding="utf-8"))
        npc.memory.set_goal_terms(["多备木材"])
        npc._last_saved_blob = None          # 绕过防抖，强制重算一次
        npc.save()
        card_after = json.loads((tmp_path / "np_memory.json").read_text(encoding="utf-8"))
        # saved_at 每次都变（它不是内容），剔除后再逐字段比
        card_before.pop("saved_at", None)
        card_after.pop("saved_at", None)
        assert card_after == card_before, "注入的目标文本不许出现在卡里"
        assert "goal_terms" not in json.dumps(card_after, ensure_ascii=False)


class TestTickInjection:
    """调度器每 tick 把活动目标文本同步进记忆层；两个开关都关则啥也不建。"""

    def _npc(self, tmp_path, world):
        return NPC(persona={"id": "g", "name": "g", "identity": "t", "personality": "t",
                            "speech_style": "t", "taboos": [],
                            "rules": {"replies": {}, "fallback": "嗯。"},
                            "routine": [], "goals": {"多备木材": {"target": 2, "action": "gather",
                                                              "params": {"resource": "木材"}}}},
                   world=world, store_dir=str(tmp_path))

    def test_injected_when_any_switch_on(self, tmp_path, monkeypatch):
        """只开 NPC_GOAL_RELEVANCE（NPC_GOALS 关）也要能注入 —— P-6 独立可用。"""
        monkeypatch.delenv("NPC_GOALS", raising=False)
        monkeypatch.setenv("NPC_GOAL_RELEVANCE", "1")
        world = _world()
        npc = self._npc(tmp_path, world)
        tick_round(world, {"g": npc}, rng=random.Random(1))
        assert npc.memory._goal_terms == ["多备木材"]

    def test_nothing_when_both_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NPC_GOALS", raising=False)
        monkeypatch.delenv("NPC_GOAL_RELEVANCE", raising=False)
        world = _world()
        npc = self._npc(tmp_path, world)
        tick_round(world, {"g": npc}, rng=random.Random(1))
        assert npc.memory._goal_terms == []
        assert getattr(npc, "_goal_queue", None) is None, "两个都关 → 连目标队列都不该建"
