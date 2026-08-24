"""中文分词检索（2026-08-23）— jieba 可选依赖, 未装时空格切词优雅回退。"""
import pytest

from npc import memory as mem


def test_tokenize_fallback_without_jieba(monkeypatch):
    """jieba 缺失 → 空格切词回退, 标点已被替换成空格。"""
    monkeypatch.setattr(mem, "jieba", None)
    tokens = mem._tokenize("昨天 森林，木头真粗。")
    assert tokens == ["昨天", "森林", "木头真粗"]


def test_tokenize_never_returns_punct_only(monkeypatch):
    """纯标点输入 → 空 token 列表（不会除零）。"""
    monkeypatch.setattr(mem, "jieba", None)
    assert mem._tokenize("。，！？") == []


@pytest.mark.skipif(mem.jieba is None, reason="未安装 jieba")
def test_jieba_splits_chinese_sentence():
    """有 jieba → 中文整句不再糊成一个词（本次改进的核心）。"""
    tokens = mem._tokenize("昨天森林里的木头真粗")
    assert len(tokens) >= 3
    assert any("木头" in t for t in tokens)


def test_relevance_chinese_query_hits():
    """中文查询的相关度: 分词后原词命中 + 同义词召回都正常工作。"""
    content = "森林里的木材又粗又直"
    # 同义词族: 木头→木材
    assert mem._relevance_score(content, "去砍点木头") > 0.0
    # 原词直接命中
    assert mem._relevance_score(content, "木材") >= 0.6


def test_memory_retrieve_regression():
    """回归: 记忆加权检索在分词改动后行为不变（同义召回主案例）。"""
    m = mem.NPCMemory()
    m.add("森林里的木材又粗又直", importance=5)
    results = m.retrieve("去砍柴", top_k=5)
    assert results and "木材" in results[0]["content"]
