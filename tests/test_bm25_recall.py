"""TDAM 借鉴④ BM25-IDF 关键词兜底检索（2026-08-26）。

开关 NPC_BM25_RECALL（默认关）: 关闭时 retrieve 与旧版同序（回归锚 B1）。
开启时: 相关度取 max(旧公式, IDF 加权命中) —— 稀有词权重大于高频词,
只升不降; 与温层向量锚点(NPC_VECTOR_ANCHOR)正交可叠加。
语料用空格分隔 ASCII 词, 保证 jieba 装与不装切词结果一致(测试确定性)。
"""
import pytest

from npc import memory as mem
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
    assert 0.0 < common < rare            # 真 BM25 分值不再归一 0-1(任务书#03)


def test_idf_relevance_edges():
    """边界: 空 query / 空语料 / 全未命中 → 0, 不抛不炸。"""
    assert _idf_relevance(set(), [], {}, 4) == 0.0
    assert _idf_relevance({"x"}, ["x"], {}, 0) == 0.0
    assert _idf_relevance(set(), ["x"], {"x": 1}, 4) == 0.0


# ── 任务书#03-C: 中文语料(jieba 装/不装两态) ──────────────────

def _memory_cn() -> NPCMemory:
    """中文空格语料(与 ASCII 版同构): 石头高频(3/4), 矿洞稀有(1/4)。
    空格分隔保证 jieba 装与不装切词结果一致(测试确定性)。"""
    m = NPCMemory()
    m.load([
        {"id": "e1", "content": "石头 木头", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "e2", "content": "石头 木头", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "e3", "content": "石头 木头", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "e4", "content": "矿洞", "importance": 0,
         "category": "general", "created_at": 1000.0},
    ])
    return m


def test_on_chinese_rare_word_wins(monkeypatch):
    """中文语料 + 开关开 → 稀有词"矿洞"条目靠 BM25 跃居第一(两态一致)。"""
    monkeypatch.setenv("NPC_BM25_RECALL", "1")
    assert _memory_cn().retrieve("石头 矿洞")[0]["id"] == "e4"


def test_off_chinese_keeps_legacy_ranking():
    """中文语料 + 开关关 → 回归锚: 稳定排序 e1 居首。"""
    assert _memory_cn().retrieve("石头 矿洞")[0]["id"] == "e1"


@pytest.mark.skipif(mem.jieba is None, reason="未安装 jieba")
def test_on_chinese_sentence_with_jieba(monkeypatch):
    """jieba 整句路径: 无空格中文, 稀有词条靠分词+BM25 翻第一。"""
    m = NPCMemory()
    m.load([
        {"id": "j1", "content": "山坡上石头很多", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "j2", "content": "河边也有很多石头", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "j3", "content": "营地的石头堆成小山", "importance": 0,
         "category": "general", "created_at": 1000.0},
        {"id": "j4", "content": "矿洞深处有道光", "importance": 0,
         "category": "general", "created_at": 1000.0},
    ])
    monkeypatch.setenv("NPC_BM25_RECALL", "1")
    assert m.retrieve("矿洞 石头")[0]["id"] == "j4"
