"""memory.py 测试 —— 含红线：uuid 不碰撞。"""
from __future__ import annotations

import json
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


# ── 检索（相关度门槛 + 相关度为主、重要度为辅）────────────

def test_retrieve_below_min_rel_not_returned(tmp_store):
    """相关度低于门槛的条目不返回 —— 哪怕重要度很高。"""
    _add(tmp_store, content="玩家爱吃面", importance=9)
    _add(tmp_store, content="今天天气不错", importance=9)

    entries = memory.retrieve("cang", "面", store_dir=tmp_store)

    assert [e["content"] for e in entries] == ["玩家爱吃面"]


def test_retrieve_importance_breaks_relevance_tie(tmp_store):
    """相关度相同时重要度高的在前。"""
    _add(tmp_store, content="面食小事", importance=1)
    _add(tmp_store, content="面食大事", importance=9)

    entries = memory.retrieve("cang", "面", store_dir=tmp_store)

    assert [e["content"] for e in entries] == ["面食大事", "面食小事"]


def test_retrieve_all_irrelevant_returns_empty(tmp_store):
    _add(tmp_store, content="玩家爱吃面", importance=9)
    assert memory.retrieve("cang", "完全无关的查询词", store_dir=tmp_store) == []


def test_retrieve_newer_first_on_full_tie(tmp_store):
    """rank 相同（相关度、重要度都一样）→ created_at 新的在前。"""
    _add(tmp_store, content="面旧", created_at=NOW - 100 * HOUR)
    _add(tmp_store, content="面新", created_at=NOW)

    entries = memory.retrieve("cang", "面", store_dir=tmp_store)

    assert [e["content"] for e in entries] == ["面新", "面旧"]


def test_relevance_is_token_hit_ratio():
    """两个 query 词命中一个 → 相关度 0.5；全命中 → 1.0（上限 1）。"""
    assert memory._relevance_score("面", "面 汤", ["面", "汤"], set(), set()) == pytest.approx(0.5)
    assert memory._relevance_score("面汤", "面 汤", ["面", "汤"], set(), set()) == pytest.approx(1.0)


def test_synonyms_boost_ranking(tmp_store):
    _add(tmp_store, content="木材")      # 无同义词表时与 query「柴」零命中 → 连门槛都过不了
    _add(tmp_store, content="石头")

    assert memory.retrieve("cang", "柴", store_dir=tmp_store) == []

    boosted = memory.retrieve("cang", "柴", synonyms={"木材": ["木材", "柴", "木头"]},
                              store_dir=tmp_store)
    assert [e["content"] for e in boosted] == ["木材"]
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
                              store_dir=tmp_store)
    assert [e["content"] for e in entries] == ["木材"]   # 只按传入的表召回


def test_retrieve_top_k_and_empty_card(tmp_store):
    for i in range(6):
        _add(tmp_store, content=f"第{i}件")
    assert len(memory.retrieve("cang", "第", top_k=2,
                               store_dir=tmp_store)) == 2
    assert memory.retrieve("nobody", "任意", store_dir=tmp_store) == []


def test_format_for_context(tmp_store):
    assert memory.format_for_context([]) == "（没有想起相关的事）"
    text = memory.format_for_context([
        {"id": "a1b2c3d4e5f6", "content": "面煮好了"},
        {"id": "f6e5d4c3b2a1", "content": "玩家在厨房"},
    ])
    assert text == "- [a1b2c3] 面煮好了\n- [f6e5d4] 玩家在厨房"


def test_tokenize_fallback_without_jieba(monkeypatch):
    monkeypatch.setattr(memory, "jieba", None)
    assert memory._tokenize("面，汤") == ["面", "汤"]          # 单字片段保留该单字
    assert memory._tokenize("锅里煮着面") == ["锅里", "里煮", "煮着", "着面"]  # 字二元组
    assert memory._tokenize("abc123 面") == ["abc123", "面"]  # ASCII 连串当整词


def test_retrieve_without_jieba_bigrams(tmp_store, monkeypatch):
    """没装 jieba 时用字二元组兜底：无空格中文 query 能检索到含相同词的条目。"""
    monkeypatch.setattr(memory, "jieba", None)
    _add(tmp_store, content="玩家上次说我爱吃面")
    _add(tmp_store, content="今天天气不错")

    entries = memory.retrieve("cang", "我爱吃面", store_dir=tmp_store)

    assert [e["content"] for e in entries] == ["玩家上次说我爱吃面"]


# ── 短编号 / resolve_id / remember_entry（replaces 更新）──

def test_resolve_id_unique_prefix(tmp_store):
    e = _add(tmp_store, content="唯一命中的记忆")
    hit = memory.resolve_id("cang", e["id"][:6], store_dir=tmp_store)
    assert hit is not None and hit["id"] == e["id"]
    assert memory.resolve_id("cang", e["id"][:5], store_dir=tmp_store)["id"] == e["id"]
    assert memory.resolve_id("cang", "没有这种编号", store_dir=tmp_store) is None
    assert memory.resolve_id("cang", "  ", store_dir=tmp_store) is None


def test_resolve_id_ambiguous_prefix_returns_none(tmp_store):
    memory.save_card("cang", [
        {"id": "aaaaaa1111", "content": "一", "importance": 5,
         "category": "general", "created_at": NOW},
        {"id": "aaaaaa2222", "content": "二", "importance": 5,
         "category": "general", "created_at": NOW},
    ], store_dir=tmp_store)
    assert memory.resolve_id("cang", "aaaaaa", store_dir=tmp_store) is None


def test_resolve_id_skips_action_entries(tmp_store):
    e = memory.add_entry("cang", "完成: 面煮好了", category="action",
                         store_dir=tmp_store)
    assert memory.resolve_id("cang", e["id"][:6], store_dir=tmp_store) is None


def test_remember_entry_replaces_updates_in_place(tmp_store):
    old = _add(tmp_store, content="玩家爱吃面", importance=7,
               category="preference", pinned=True)

    entry, replaced = memory.remember_entry("cang", "玩家喜欢面条",
                                            replaces=old["id"][:6],
                                            store_dir=tmp_store)

    assert replaced is True
    assert entry["id"] == old["id"]                    # 原 id 保留
    assert entry["created_at"] == pytest.approx(old["created_at"])  # 创建时间保留
    assert entry["pinned"] is True                     # pinned 保留
    assert entry["content"] == "玩家喜欢面条"
    assert entry["importance"] == 7                    # 没传 importance → 保留原值
    assert entry["category"] == "preference"
    assert len(memory.load_card("cang", store_dir=tmp_store)) == 1   # 原地更新，不新增


def test_remember_entry_replaces_with_explicit_fields(tmp_store):
    old = _add(tmp_store, content="玩家爱吃面", importance=7, category="preference")

    entry, replaced = memory.remember_entry("cang", "玩家喜欢面条", importance=3,
                                            category="general",
                                            replaces=old["id"][:6],
                                            store_dir=tmp_store)

    assert replaced is True
    assert entry["importance"] == 3 and entry["category"] == "general"


def test_remember_entry_replaces_unresolved_adds_new(tmp_store):
    old = _add(tmp_store, content="玩家爱吃面", importance=7)

    entry, replaced = memory.remember_entry("cang", "玩家喜欢面条",
                                            replaces="没有这种编号",
                                            store_dir=tmp_store)

    assert replaced is False
    assert entry["id"] != old["id"]
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "玩家爱吃面", "玩家喜欢面条"]


def test_remember_entry_without_replaces_adds(tmp_store):
    entry, replaced = memory.remember_entry("cang", "新记忆", store_dir=tmp_store)
    assert replaced is False
    assert entry["importance"] == memory.DEFAULT_IMPORTANCE
    assert entry["category"] == memory.DEFAULT_CATEGORY


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



