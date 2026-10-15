"""memory.py 测试 —— 含两条红线：uuid 不碰撞、pinned 豁免修剪。"""
from __future__ import annotations

import json
import time
import uuid

import pytest

import memory


NOW = 1_800_000_000.0
HOUR = 3600.0


def _add(store, npc="cang", content="记一条", importance=5, category="general",
         pinned=False, created_at=None):
    """直接落一条带指定 created_at 的条目（绕过 add_entry 的时间戳）。"""
    entries = memory.load_card(npc, store_dir=store)
    entries.append({
        "id": uuid.uuid4().hex,
        "content": content,
        "importance": importance,
        "category": category,
        "created_at": NOW if created_at is None else created_at,
        **({"pinned": True} if pinned else {}),
    })
    memory.save_card(npc, entries, store_dir=store)
    return entries[-1]


# ── 红线 ─────────────────────────────────────────────────

def test_uuid_ids_do_not_collide(tmp_store):
    ids = [memory.add_entry("cang", f"第 {i} 件事", store_dir=tmp_store)["id"]
           for i in range(1000)]
    assert len(set(ids)) == 1000
    for i in ids:
        assert uuid.UUID(hex=i)          # 合法 uuid


def test_pinned_entry_survives_prune(tmp_store):
    _add(tmp_store, content="钉住的话", importance=0, pinned=True,
         created_at=NOW - 100000 * HOUR)
    _add(tmp_store, content="没钉的弱话", importance=1, created_at=NOW - 100000 * HOUR)

    removed = memory.prune("cang", now=NOW, store_dir=tmp_store)

    contents = [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)]
    assert "钉住的话" in contents            # 红线：pinned 豁免一切自动修剪
    assert "没钉的弱话" not in contents
    assert removed == 1


# ── 半衰期修剪（强度 < 1.0 的旧条目被移除）───────────────

def test_prune_half_life_thresholds(tmp_store):
    _add(tmp_store, content="72小时中等", importance=5, created_at=NOW - 72 * HOUR)      # 2.5 保留
    _add(tmp_store, content="200小时中等", importance=5, created_at=NOW - 200 * HOUR)    # 0.73 移除
    _add(tmp_store, content="高重要度", importance=8, created_at=NOW - 100000 * HOUR)     # 豁免
    _add(tmp_store, content="反思产物", importance=1, category="reflection",
         created_at=NOW - 100000 * HOUR)                                     # 豁免
    _add(tmp_store, content="合并产物", importance=1, category="consolidated",
         created_at=NOW - 100000 * HOUR)                                     # 豁免

    removed = memory.prune("cang", now=NOW, store_dir=tmp_store)

    contents = {e["content"] for e in memory.load_card("cang", store_dir=tmp_store)}
    assert contents == {"72小时中等", "高重要度", "反思产物", "合并产物"}
    assert removed == 1


def test_prune_empty_card_is_noop(tmp_store):
    assert memory.prune("nobody", now=NOW, store_dir=tmp_store) == 0


# ── 检索（AI Town 加权公式）───────────────────────────────

def test_retrieve_recency_wins_on_equal_others(tmp_store):
    _add(tmp_store, content="旧事", importance=5, created_at=NOW - 100 * HOUR)
    _add(tmp_store, content="新事", importance=5, created_at=NOW)

    assert memory.retrieve("cang", "无关查询", now=NOW,
                           store_dir=tmp_store)[0]["content"] == "新事"


def test_retrieve_importance_wins_on_equal_others(tmp_store):
    _add(tmp_store, content="小事", importance=1)
    _add(tmp_store, content="大事", importance=9)

    assert memory.retrieve("cang", "无关查询", now=NOW,
                           store_dir=tmp_store)[0]["content"] == "大事"


def test_retrieve_relevance_wins_on_equal_others(tmp_store):
    _add(tmp_store, content="今天天气不错")
    _add(tmp_store, content="锅里煮着面")

    assert memory.retrieve("cang", "面", now=NOW,
                           store_dir=tmp_store)[0]["content"] == "锅里煮着面"


def test_relevance_is_token_hit_ratio():
    """两个 query 词命中一个 → 相关度 0.5（权重 3 → 贡献 1.5）。"""
    assert memory._relevance_score("面", "面 汤", ["面", "汤"], set(), set()) == pytest.approx(0.5)
    assert memory._relevance_score("面汤", "面 汤", ["面", "汤"], set(), set()) == pytest.approx(1.0)


def test_synonyms_boost_ranking(tmp_store):
    _add(tmp_store, content="木材")      # 无同义词表时与 query「柴」零命中
    _add(tmp_store, content="石头")

    baseline = memory.retrieve("cang", "柴", now=NOW, store_dir=tmp_store)
    assert baseline[0]["content"] == "木材"      # 同分时保持卡内顺序

    boosted = memory.retrieve("cang", "柴", synonyms={"木材": ["木材", "柴", "木头"]},
                              now=NOW, store_dir=tmp_store)
    assert boosted[0]["content"] == "木材"
    assert memory._relevance_score("木材", "柴", ["柴"], {"木材"}, {"木材"}) == pytest.approx(1.0)


def test_synonyms_come_only_from_explicit_table(tmp_store):
    """同义词表只认显式传入的那份 —— 进程级全局表已删（P-24 阶段 2）。

    多 NPC 并发口径：除了调用方传进来的表，检索不看任何别处 ——
    A 的同义词表没有可能"留在什么地方"污染 B 的检索。
    """
    assert not hasattr(memory, "set_synonyms")      # 全局表入口已删
    assert not hasattr(memory, "_ACTIVE_SYNONYMS")  # 全局表本身已删
    _add(tmp_store, content="木材")
    _add(tmp_store, content="石头")

    entries = memory.retrieve("cang", "柴", synonyms={"木材": ["木材", "柴"]},
                              now=NOW, store_dir=tmp_store)
    assert entries[0]["content"] == "木材"        # 只按传入的表召回


def test_retrieve_top_k_and_empty_card(tmp_store):
    for i in range(6):
        _add(tmp_store, content=f"第{i}件")
    assert len(memory.retrieve("cang", "第", top_k=2, now=NOW,
                               store_dir=tmp_store)) == 2
    assert memory.retrieve("nobody", "任意", now=NOW, store_dir=tmp_store) == []


def test_format_for_context(tmp_store):
    assert memory.format_for_context([]) == "（没有想起相关的事）"
    text = memory.format_for_context([{"content": "面煮好了"}, {"content": "玩家在厨房"}])
    assert text == "- 面煮好了\n- 玩家在厨房"


def test_tokenize_fallback_without_jieba(monkeypatch):
    monkeypatch.setattr(memory, "jieba", None)
    assert memory._tokenize("面，汤") == ["面", "汤"]


# ── 兼容旧卡 / 往返 / 手改 ─────────────────────────────────

def test_load_legacy_card_assigns_uuid(tmp_store):
    legacy = [
        {"content": "旧版条目一", "importance": 7, "created_at": NOW},
        {"content": "旧版条目二", "importance": 3, "created_at": NOW, "id": "mem_1_1700000000"},
    ]
    memory.card_path("old", store_dir=tmp_store).write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    entries = memory.load_card("old", store_dir=tmp_store)

    assert entries[0]["category"] == "general"
    assert uuid.UUID(hex=entries[0]["id"])            # 缺 id → 补 uuid
    assert entries[1]["id"] == "mem_1_1700000000"     # 已有 id 不动
    assert [e["importance"] for e in entries] == [7, 3]
    assert "pinned" not in entries[0]


def test_save_load_roundtrip_and_hand_edit(tmp_store):
    memory.add_entry("cang", "手改测试", importance=5, store_dir=tmp_store)
    memory.add_entry("cang", "第二条", importance=3, store_dir=tmp_store)
    before = memory.load_card("cang", store_dir=tmp_store)
    assert [e["content"] for e in before] == ["手改测试", "第二条"]

    raw = json.loads(memory.card_path("cang", store_dir=tmp_store).read_text("utf-8"))
    raw[0]["importance"] = 9
    raw[0]["pinned"] = True
    memory.card_path("cang", store_dir=tmp_store).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    after = memory.load_card("cang", store_dir=tmp_store)
    assert after[0]["importance"] == 9 and after[0]["pinned"] is True


def test_importance_is_clamped(tmp_store):
    assert memory.add_entry("cang", "超范围", importance=42,
                            store_dir=tmp_store)["importance"] == 9
    assert memory.add_entry("cang", "负数", importance=-3,
                            store_dir=tmp_store)["importance"] == 0


# ── 动作结果写卡 ──────────────────────────────────────────

def test_add_action_result_wording(tmp_store):
    ok = memory.add_action_result("cang", "cook", True, "面煮好了，玩家说不错",
                                  store_dir=tmp_store)
    fail = memory.add_action_result("cang", "goto", False, "", store_dir=tmp_store)

    assert ok["content"] == f"{memory.EV_DONE}面煮好了，玩家说不错"
    assert ok["importance"] == 6 and ok["category"] == "action"
    assert fail["content"] == f"{memory.EV_FAIL}goto"
    assert fail["importance"] == 4


# ── 边界：响亮失败 ───────────────────────────────────────

def test_empty_content_rejected(tmp_store):
    with pytest.raises(ValueError):
        memory.add_entry("cang", "   ", store_dir=tmp_store)


def test_broken_json_raises_with_path(tmp_store):
    memory.card_path("cang", store_dir=tmp_store).write_text("{不是数组", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        memory.load_card("cang", store_dir=tmp_store)
    assert "cang_memory.json" in str(exc.value)

    memory.card_path("cang", store_dir=tmp_store).write_text('{"a": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        memory.load_card("cang", store_dir=tmp_store)


def test_entry_without_content_rejected(tmp_store):
    memory.card_path("cang", store_dir=tmp_store).write_text(
        json.dumps([{"importance": 5}]), encoding="utf-8")
    with pytest.raises(ValueError):
        memory.load_card("cang", store_dir=tmp_store)


def test_prune_keeps_recent_entries(tmp_store):
    """用真实时间轴：刚记的条目强度 5.0，不会被修剪。"""
    memory.add_entry("cang", "刚记的", importance=5, store_dir=tmp_store)
    assert memory.prune("cang", store_dir=tmp_store) == 0
    assert len(memory.load_card("cang", store_dir=tmp_store)) == 1
