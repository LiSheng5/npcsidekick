"""阶段③ 遗忘合并测试 — 重复主题合并 + 弱旧修剪 + 反思进度重置。"""
import time

import pytest

from npc.memory import NPCMemory
from npc.npc import NPC


class TestMerge:
    """重复主题合并成一条（模式留存，流水账淡去）。"""

    def test_merges_repeated_topic(self):
        mem = NPCMemory()
        mem.add("帮主角采了 2 根木材", importance=5)
        mem.add("又帮主角砍了几根柴", importance=5)
        mem.add("给主角捡了木头", importance=5)
        removed = mem.consolidate(min_group=3)
        assert removed == 3
        entries = mem.all()
        assert len(entries) == 1
        assert entries[0]["category"] == "consolidated"
        assert "木材" in entries[0]["content"]
        assert "3 次" in entries[0]["content"]

    def test_below_group_no_merge(self):
        mem = NPCMemory()
        mem.add("帮主角采了 2 根木材", importance=5)
        mem.add("河边的石头很滑", importance=5)
        assert mem.consolidate(min_group=3) == 0
        assert len(mem.all()) == 2


class TestPrune:
    """弱旧记忆按半衰期强度修剪。"""

    def test_prunes_weak_old(self):
        mem = NPCMemory()
        mem.add("一件旧小事", importance=2)
        mem.entries[-1]["created_at"] = time.time() - 72 * 3600 * 5   # 15 天前
        mem.add("新鲜的重要事", importance=7)
        removed = mem.consolidate(prune_strength=1.0)
        assert removed == 1
        contents = [x["content"] for x in mem.all()]
        assert "一件旧小事" not in contents
        assert "新鲜的重要事" in contents

    def test_keeps_reflection_and_high_importance(self):
        mem = NPCMemory()
        mem.add("旧反思结论", importance=8, category="reflection")
        mem.entries[-1]["created_at"] = time.time() - 72 * 3600 * 10
        mem.add("旧高重要事实", importance=8)
        mem.entries[-1]["created_at"] = time.time() - 72 * 3600 * 10
        mem.add("旧低重要", importance=2)
        mem.entries[-1]["created_at"] = time.time() - 72 * 3600 * 10
        removed = mem.consolidate(prune_strength=1.0)
        assert removed == 1
        contents = [x["content"] for x in mem.all()]
        assert "旧反思结论" in contents
        assert "旧高重要事实" in contents
        assert "旧低重要" not in contents


class TestNPCWrapper:
    """NPC 层包装: 合并后反思进度重置，避免重复处理。"""

    def test_consolidate_resets_reflection_marker(self):
        npc = NPC(store_dir="npc/store_test")
        npc.use_llm = False
        npc.remember("帮主角采了 2 根木材", importance=6)
        npc.remember("帮主角修了屋顶", importance=7)
        npc.maybe_reflect()          # 反思 → reflected_upto 前进
        npc.memory.add("河边石头滑", importance=5)
        npc.consolidate()
        assert npc._reflected_upto == len(npc.memory.all())  # 重置到末尾

    def test_consolidate_saves_when_removed(self):
        npc = NPC(store_dir="npc/store_test")
        npc.use_llm = False
        npc.remember("帮主角采了 2 根木材", importance=5)
        npc.remember("又帮主角砍了几根柴", importance=5)
        npc.remember("给主角捡了木头", importance=5)
        removed = npc.consolidate()   # 默认 min_group=3 → 合并
        assert removed == 3
        assert len(npc.memory.all()) == 1
