"""阶段② 关联检索测试 — 同义词召回 + 一跳关联（轻量海马体）。"""
import pytest

from npc.memory import NPCMemory, _canonical_terms


class TestCanonicalTerms:
    """规范词抽取: 同义词族归一到规范词。"""

    def test_synonym_family(self):
        assert _canonical_terms("去砍柴") == {"木材"}
        assert _canonical_terms("摘了一把野果") == {"浆果"}
        assert _canonical_terms("矿洞里的岩石") == {"石头", "矿洞"}

    def test_no_entities(self):
        assert _canonical_terms("今天天气不错") == set()


class TestSynonymRecall:
    """中文同义召回: "柴" 能召回 "木材" 的记忆（阶段② 核心）。"""

    def test_synonym_query_matches(self):
        mem = NPCMemory()
        mem.add("森林里的木材又粗又直", importance=5)
        mem.add("矿洞里的石头很硬", importance=5)
        results = mem.retrieve("去砍柴", top_k=5)   # "柴" → 木材
        assert "木材又粗" in results[0]["content"]

    def test_synonym_in_memory_matches_query(self):
        mem = NPCMemory()
        mem.add("帮主角捡了一堆柴", importance=5)   # 记忆用"柴"，查询用"木材"
        mem.add("河边的石头很滑", importance=5)
        results = mem.retrieve("需要木材", top_k=5)
        assert "捡了一堆柴" in results[0]["content"]


class TestAssociation:
    """一跳关联: 通过共现实体找到"拐弯相关"的记忆（轻量海马体）。"""

    def test_association_via_shared_entity(self):
        mem = NPCMemory()
        mem.add("苍帮主角采了 2 根木材", importance=6)   # 苍+主角 共现
        mem.add("主角的家里堆着很多柴", importance=4)     # 主角相关（经主角关联）
        mem.add("河边的石头很滑", importance=3)          # 无关
        results = mem.retrieve("苍怎么样", top_k=3)
        contents = [r["content"] for r in results]
        assert "苍帮主角" in contents[0]
        assert "家里堆着很多柴" in contents[1]

    def test_no_association_without_entity_query(self):
        mem = NPCMemory()
        mem.add("苍帮主角采了 2 根木材", importance=6)
        mem.add("主角的家里堆着很多柴", importance=6)
        # 无实体查询 → 不触发关联，只按原词相关度
        results = mem.retrieve("天气怎么样", top_k=3)
        assert len(results) == 2
