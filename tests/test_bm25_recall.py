"""TDAM 借鉴④ BM25-IDF 关键词兜底检索（2026-08-26）。

开关 NPC_BM25_RECALL（默认关）: 关闭时 retrieve 与旧版同序（回归锚 B1）。
开启时: 相关度取 max(旧公式, IDF 加权命中) —— 稀有词权重大于高频词,
只升不降; 与温层向量锚点(NPC_VECTOR_ANCHOR)正交可叠加。
语料用空格分隔 ASCII 词, 保证 jieba 装与不装切词结果一致(测试确定性)。
"""
import pytest

from npc.memory import NPCMemory, _idf_relevance


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NPC_BM25_RECALL", raising=False)
    monkeypatch.delenv("NPC_VECTOR_ANCHOR", raising=False)


def _memory() -> NPCMemory:
    """固定语料: wood 高频(3/4), mine 稀有(1/4); 时间/重要度全对齐隔离变量。"""
    m = NPCMemory()
    m.load([
        {"id": "e1", "content": "wood stone", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "e2", "content": "wood berry", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "e3", "content": "wood rope", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "e4", "content": "mine", "importance": 0,
         "category": "general", "created_at": 1000.0},
    ])
    return m


def test_off_keeps_legacy_ranking():
    """回归锚: 开关关 → 四条同分, 稳定排序保持插入顺序, e1 居首。"""
    assert _memory().retrieve("wood mine")[0]["id"] == "e1"


def test_on_rare_word_wins(monkeypatch):
    """开关开 → 只含稀有词 mine 的 e4 靠 IDF 权重跃居第一。"""
    monkeypatch.setenv("NPC_BM25_RECALL", "1")
    assert _memory().retrieve("wood mine")[0]["id"] == "e4"


def test_idf_relevance_rare_beats_common():
    """单元: 同样命中一个词, 命中稀有词的得分严格高于高频词。"""
    df = {"wood": 3, "mine": 1}
    common = _idf_relevance({"wood"}, ["wood", "mine"], df, 4)
    rare = _idf_relevance({"mine"}, ["wood", "mine"], df, 4)
    assert 0.0 < common < rare <= 1.0


def test_idf_relevance_edges():
    """边界: 空 query / 空语料 / 全未命中 → 0, 不抛不炸。"""
    assert _idf_relevance(set(), [], {}, 4) == 0.0
    assert _idf_relevance({"x"}, ["x"], {}, 0) == 0.0
    assert _idf_relevance(set(), ["x"], {"x": 1}, 4) == 0.0
