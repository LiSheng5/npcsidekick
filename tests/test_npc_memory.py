"""NPC 记忆测试 — AI Town 加权公式的确定性验证。"""
import time

from npc.memory import NPCMemory


class TestAddAndRetrieve:
    def test_add_returns_id(self):
        mem = NPCMemory()
        mem_id = mem.add("俺帮主角采了木材", importance=8)
        assert mem_id.startswith("mem_")
        assert len(mem.all()) == 1

    def test_retrieve_empty(self):
        mem = NPCMemory()
        assert mem.retrieve("木材") == []


class TestWeighting:
    def test_importance_wins_over_recency(self):
        """旧的高重要性记忆 > 新的低重要性记忆（相关度相同时）。"""
        mem = NPCMemory()
        old_important = mem.add("主角的新房需要 20 根木材", importance=9)
        # 伪造时间: 让新条目其实是"很久以前"
        mem.entries[0]["created_at"] = time.time() - 24 * 3600  # 1 天前
        mem.add("昨天吃了烤土豆", importance=1)
        results = mem.retrieve("木材", top_k=5)
        assert results[0]["id"] == old_important

    def test_relevance_boosts(self):
        """相关度命中 > 未命中（重要性相近时）。"""
        mem = NPCMemory()
        mem.add("森林里的木材又粗又直", importance=5)
        mem.add("矿洞里的石头很硬", importance=5)
        results = mem.retrieve("木材", top_k=5)
        assert "木材又粗" in results[0]["content"]

    def test_top_k_limits(self):
        mem = NPCMemory()
        for i in range(10):
            mem.add(f"记忆 {i}", importance=5)
        assert len(mem.retrieve("记忆", top_k=3)) == 3


class TestDecay:
    def test_older_memory_scores_lower(self):
        mem = NPCMemory()
        mem.add("新鲜记忆", importance=5)
        mem.entries[0]["created_at"] = time.time() - 24 * 3600  # 一天前
        fresh = mem.add("刚刚的事", importance=5)
        results = mem.retrieve("事", top_k=5)
        assert results[0]["id"] == fresh


class TestPersistence:
    def test_to_dict_load_roundtrip(self):
        mem = NPCMemory()
        mem.add("俺记得给主角采过木材", importance=8)
        data = mem.to_dict()
        restored = NPCMemory()
        restored.load(data)
        assert restored.all()[0]["content"] == "俺记得给主角采过木材"
        assert restored.all()[0]["importance"] == 8

    def test_format_for_context(self):
        mem = NPCMemory()
        assert "还没有记忆" in mem.format_for_context()
        mem.add("俺记得给主角采过木材")
        text = mem.format_for_context()
        assert "俺记得给主角采过木材" in text
