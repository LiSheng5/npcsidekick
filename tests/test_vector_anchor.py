"""温层向量锚点(P1·2026-08-25)测试 — 语义检索增强, NPC_VECTOR_ANCHOR 开关。

全部使用注入式 Fake 向量存储 —— 零 chromadb 依赖、零网络、零模型下载。
覆盖: 关闭零接触 / 写入镜像 / 语义加分翻转排序 / 旧卡自愈回填 /
不可用降级 / consolidate 出索引联动。
"""
from types import SimpleNamespace

import pytest

from npc.memory import NPCMemory


class FakeVS:
    """向量存储协议的最小假体(available/count/add/search/delete)。"""

    def __init__(self, hits=None, available=True):
        self.available_flag = available
        self.calls = []
        self.added = []      # [(doc_id, text)]
        self.deleted = []
        self._hits = hits

    @property
    def available(self):
        return self.available_flag

    def count(self):
        return len(self.added)

    def add(self, doc_id, text, metadata):
        self.calls.append(("add", doc_id))
        self.added.append((doc_id, text))

    def search(self, query, top_k=8, threshold=None):
        self.calls.append(("search", query))
        return self._hits

    def add_batch(self, items):
        for doc_id, text, _meta in items:
            self.calls.append(("add", doc_id))
            self.added.append((doc_id, text))

    def delete(self, doc_id):
        self.deleted.append(doc_id)


def _hit(text, score=0.9):
    return SimpleNamespace(text=text, score=score)


# ── 开关语义 ─────────────────────────────────────────────────

def test_anchor_off_means_zero_contact(monkeypatch):
    monkeypatch.delenv("NPC_VECTOR_ANCHOR", raising=False)
    fake = FakeVS()
    mem = NPCMemory(anchor_dir="x", vector_store=fake)
    mem.add("学会用打火机", importance=5)
    mem.retrieve("打火机")
    assert fake.calls == []              # 关闭 → 存储零接触(旧行为逐字节一致)


def test_anchor_on_mirrors_writes(monkeypatch):
    monkeypatch.setenv("NPC_VECTOR_ANCHOR", "1")
    fake = FakeVS()
    mem = NPCMemory(anchor_dir="x", vector_store=fake)
    mem.add("我学会了时间管理", importance=5)
    assert any(c[0] == "add" for c in fake.calls)
    assert any(t == "我学会了时间管理" for _, t in fake.added)


# ── 语义加分: 确定性翻转 ──────────────────────────────────────

def test_anchor_flips_ranking_via_semantics(monkeypatch):
    """查询与两条记忆零字面重叠 → 基线按插入序；开启后语义命中者翻到第一。"""
    # 先关: 插入序 B(浆果) 在前
    monkeypatch.delenv("NPC_VECTOR_ANCHOR", raising=False)
    base = NPCMemory(anchor_dir="x", vector_store=FakeVS())
    base.add("浆果真甜", importance=5)
    base.add("我学会了时间管理", importance=5)
    for e in base.all():
        e["created_at"] = 1000.0           # 冻结时戳: 消除新近度抖动, 排序全凭锚点
    q = "今天天气怎么样"                   # 与两条内容零字符重叠 → 相关度全 0
    assert [e["content"] for e in base.retrieve(q, top_k=2)][0] == "浆果真甜"
    # 后开: 同数据同查询, 语义命中(时间管理)翻到第一
    monkeypatch.setenv("NPC_VECTOR_ANCHOR", "1")
    fake = FakeVS(hits=[_hit("我学会了时间管理", 0.9)])
    mem = NPCMemory(anchor_dir="x", vector_store=fake)
    mem.add("浆果真甜", importance=5)
    mem.add("我学会了时间管理", importance=5)
    for e in mem.all():
        e["created_at"] = 1000.0           # 同样冻结: 差异只可能来自锚点加权
    got = [e["content"] for e in mem.retrieve(q, top_k=2)]
    assert got[0] == "我学会了时间管理"     # 锚点加权翻转排序


# ── 回填与生命周期联动 ────────────────────────────────────────

def test_backfill_heals_legacy_entries(monkeypatch):
    """索引为空而卡有条目 → 首次启用批量回填(旧卡自愈迁移)。"""
    monkeypatch.setenv("NPC_VECTOR_ANCHOR", "1")
    fake = FakeVS()
    mem = NPCMemory(anchor_dir="x", vector_store=fake)
    mem.entries.append({"id": "mem_old_1", "content": "旧事一",
                        "importance": 5, "category": "general",
                        "created_at": 111.0})
    mem.entries.append({"id": "mem_old_2", "content": "旧事二",
                        "importance": 6, "category": "general",
                        "created_at": 222.0})
    mem.retrieve("随便问问")                # 触发回填
    assert {t for _, t in fake.added} == {"旧事一", "旧事二"}
    mem.retrieve("再问一次")                # 二次检索不再重复回填
    assert sum(1 for c in fake.calls if c[0] == "add") == 2


def test_unavailable_store_degrades_silently(monkeypatch):
    monkeypatch.setenv("NPC_VECTOR_ANCHOR", "1")
    fake = FakeVS(available=False)
    mem = NPCMemory(anchor_dir="x", vector_store=fake)
    mem.add("学会用打火机")
    r = mem.retrieve("打火机")
    assert r and fake.calls == []          # 不可用 → 完全旁路


def test_consolidate_removals_sync_out_of_index(monkeypatch):
    """被修剪的条目同步从向量索引删除(gone-diff 联动)。"""
    monkeypatch.setenv("NPC_VECTOR_ANCHOR", "1")
    fake = FakeVS()
    mem = NPCMemory(anchor_dir="x", vector_store=fake)
    mem.add("陈年小事一", importance=3)
    mem.add("陈年小事二", importance=3)
    ids = {d for d, _ in fake.added}
    # 把两条拨成 400 天前的弱记忆 → 强度远低于修剪阈值
    for e in mem.all():
        e["created_at"] -= 400 * 24 * 3600
    removed = mem.consolidate()
    assert removed >= 2
    assert ids <= set(fake.deleted)        # 索引同步出清
