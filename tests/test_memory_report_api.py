"""记忆体检 / 整理审计流水 只读 API 测试（2026-09-17）。

契约:
  · GET /api/memory/report → survey() 结构化 dict（per_npc / global），零 LLM
      - top_n 非法（非整数）回落默认 10，越界夹到 1..100
      - 空记忆的 NPC 世界不炸
  · GET /api/npcs/{pid}/memory-journal → 读该 NPC 的 {id}_report.jsonl
      - 无文件 → 空列表；有文件 → 返回尾部 N 条且顺序正确（最新在后）
      - 坏行跳过（不 500）；pid 不存在 → 404
  · 两个端点都带 _verify_origin（Origin 校验仍生效）
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from npc import housekeeper as hk
from npc.npc import NPC
from npc.server import create_npc_server


@pytest.fixture
def client(tmp_path):
    npc = NPC(store_dir=str(tmp_path / "store"))
    npc.use_llm = False
    app = create_npc_server({"cang": npc}, config_dir=str(tmp_path / "config"))
    return TestClient(app)


# ── /api/memory/report ───────────────────────────────────────

def test_report_200_and_structure(client):
    """200 + 结构含 per_npc / global；空记忆 NPC（空世界）不炸。"""
    r = client.get("/api/memory/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "per_npc" in body and "global" in body
    assert isinstance(body["per_npc"], dict)
    assert isinstance(body["global"], dict)
    g = body["global"]
    # cang 在册（空记忆 → npc_count=1, total_entries=0, 不崩）
    assert g["npc_count"] == 1
    assert g["total_entries"] == 0
    # 空记忆 NPC 仍进 tidy_ranking（只是超阈/未分型全 0），top_duplicates 因无重复为空
    assert [x["actor_id"] for x in g["tidy_ranking"]] == ["cang"]
    assert g["top_duplicates"] == []


def test_report_top_n_invalid_falls_back(client):
    """top_n=abc（非整数）→ 回落默认 10 → top_duplicates 至多 10 条。"""
    npc = NPC(store_dir="npc/store_test_report_a")
    npc.use_llm = False
    # 造 30 条互不相同的重复内容（每条 times=2）→ 全局重复榜最多 30 条
    entries = []
    for i in range(30):
        c = f"重复内容_{i:03d}"
        entries.append({"id": f"a{i}", "content": c, "category": "general",
                        "created_at": float(i)})
        entries.append({"id": f"b{i}", "content": c, "category": "general",
                        "created_at": float(i) + 0.5})
    npc.memory.load(entries)
    app = create_npc_server({"cang": npc})
    c = TestClient(app)
    r = c.get("/api/memory/report?top_n=abc")
    assert r.status_code == 200, r.text
    assert len(r.json()["global"]["top_duplicates"]) == 10


def test_report_top_n_clamped_to_100(client):
    """top_n=500（越界）→ 夹到 100；top_n=5 → 5；top_n=0 → 夹到 1。"""
    npc = NPC(store_dir="npc/store_test_report_b")
    npc.use_llm = False
    entries = []
    for i in range(150):
        c = f"重复内容_{i:03d}"
        entries.append({"id": f"a{i}", "content": c, "category": "general",
                        "created_at": float(i)})
        entries.append({"id": f"b{i}", "content": c, "category": "general",
                        "created_at": float(i) + 0.5})
    npc.memory.load(entries)
    app = create_npc_server({"cang": npc})
    c = TestClient(app)

    r = c.get("/api/memory/report?top_n=500")
    assert r.status_code == 200, r.text
    assert len(r.json()["global"]["top_duplicates"]) == 100

    r = c.get("/api/memory/report?top_n=5")
    assert r.status_code == 200, r.text
    assert len(r.json()["global"]["top_duplicates"]) == 5

    r = c.get("/api/memory/report?top_n=0")
    assert r.status_code == 200, r.text
    assert len(r.json()["global"]["top_duplicates"]) == 1


# ── /api/npcs/{pid}/memory-journal ────────────────────────────

def _write_journal(npc, records, *, bad_line=None):
    p: Path = hk.report_path(npc)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(__import__("json").dumps(rec, ensure_ascii=False) + "\n")
        if bad_line is not None:
            fh.write(bad_line + "\n")          # 故意写一行坏 JSON


def test_journal_no_file_returns_empty(client):
    """新 NPC 没有 report 文件 → 返回空列表，不报错。"""
    r = client.get("/api/npcs/cang/memory-journal")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_journal_returns_tail_in_order(tmp_path):
    """有文件 → 返回尾部 N 条且顺序正确（最新在后）；默认 limit=50 取全部。"""
    npc = NPC(store_dir=str(tmp_path / "store"))
    npc.use_llm = False
    recs = [{"at": f"t{i}", "trigger": "minor", "npc": "cang",
             "actions": [{"op": "demote", "id": i}]} for i in range(5)]
    _write_journal(npc, recs)

    app = create_npc_server({"cang": npc}, config_dir=str(tmp_path / "config"))
    c = TestClient(app)

    r = c.get("/api/npcs/cang/memory-journal")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 5
    assert [b["at"] for b in body] == ["t0", "t1", "t2", "t3", "t4"]

    r = c.get("/api/npcs/cang/memory-journal?limit=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [b["at"] for b in body] == ["t3", "t4"]


def test_journal_bad_line_skipped(tmp_path):
    """坏行跳过（不 500）；有效行仍按序返回。"""
    npc = NPC(store_dir=str(tmp_path / "store"))
    npc.use_llm = False
    recs = [{"at": "t0", "actions": []}, {"at": "t1", "actions": []}]
    _write_journal(npc, recs, bad_line="{这不是合法json")
    app = create_npc_server({"cang": npc}, config_dir=str(tmp_path / "config"))
    c = TestClient(app)

    r = c.get("/api/npcs/cang/memory-journal")
    assert r.status_code == 200, r.text          # 不因坏行 500
    body = r.json()
    assert [b["at"] for b in body] == ["t0", "t1"]


def test_journal_unknown_pid_404(client):
    """不存在的 pid → 404（照 get_npc 行为）。"""
    r = client.get("/api/npcs/nobody/memory-journal")
    assert r.status_code == 404


def test_journal_path_does_not_collide_with_memory_endpoint(client):
    """回归锚: 新路径 200，且紧邻的既有 `/api/npcs/{pid}/memory`(GET) 仍 200。

    证明复数前缀 + 4 段（`memory-journal`）没撞坏记忆家族邻居，
    也证明不会与将来的 `/api/npcs/{pid}/memory/{mid}` 模式冲突。
    """
    r = client.get("/api/npcs/cang/memory-journal")
    assert r.status_code == 200, r.text
    r2 = client.get("/api/npcs/cang/memory")       # 既有 console_api 端点
    assert r2.status_code == 200, r2.text


# ── Origin 校验仍生效 ────────────────────────────────────────

def test_report_bad_origin_rejected(client):
    r = client.get("/api/memory/report", headers={"Origin": "http://evil.com"})
    assert r.status_code == 403


def test_journal_bad_origin_rejected(client):
    r = client.get("/api/npcs/cang/memory-journal",
                   headers={"Origin": "http://evil.com"})
    assert r.status_code == 403
