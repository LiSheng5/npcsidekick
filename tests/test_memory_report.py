"""记忆体检报告测试（任务背景: 给用户看的全局视图, 纯规则零 LLM）。

钉住: 空/单/多 NPC、重复检测(count 字段也能识别)、未分型计数、超阈值判定、
`--json` 纯度(子进程 + json.loads 必过)、流民单列跳过。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from npc import memory_report as mr
from npc.npc import NPC

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离阈值/去重等环境变量, 防止跨测试污染统计口径。"""
    monkeypatch.delenv("NPC_MEMORY_TOKEN_MAX", raising=False)
    monkeypatch.delenv("NPC_MEMORY_DEDUP", raising=False)
    monkeypatch.delenv("NPC_VECTOR_ANCHOR", raising=False)


def _npc(tmp_path, actor_id="a1", ephemeral=False):
    return NPC(persona={"id": actor_id, "name": "体检测"},
               store_dir=str(tmp_path), ephemeral=ephemeral)


def _load(npc, entries):
    """直接灌入精确条目(绕过 remember/dedup), 控制 mtype/category/count/created_at。"""
    npc.memory.load(entries)
    return npc


# ── 空 / 单 / 多 NPC ─────────────────────────────────────────

def test_empty_npcs(tmp_path):
    r = mr.survey({})
    assert r["per_npc"] == {}
    g = r["global"]
    assert g["npc_count"] == 0 and g["total_entries"] == 0
    assert g["total_tokens"] == 0 and g["over_threshold_count"] == 0
    assert g["tidy_ranking"] == [] and g["top_duplicates"] == []


def test_single_npc_counts(tmp_path):
    npc = _load(_npc(tmp_path), [
        {"id": "1", "content": "甲", "importance": 5, "category": "general",
         "created_at": 100.0, "mtype": "persona"},
        {"id": "2", "content": "乙", "importance": 5, "category": "general",
         "created_at": 200.0},                       # 无 mtype → 未分型
        {"id": "3", "content": "丙", "importance": 5, "category": "archived",
         "created_at": 300.0},                       # 归档(退出活跃口径)
    ])
    r = mr.survey({"a1": npc}, now=1000.0)
    s = r["per_npc"]["a1"]
    assert s["total"] == 3 and s["active"] == 2 and s["archived"] == 1
    assert s["tokens"] == 2                          # 只算 active(甲+乙)
    assert s["mtype_distribution"] == {"persona": 1, "episodic": 0, "instruction": 0}
    assert s["untyped"] == 1 and s["untyped_ratio"] == 0.5
    # 最旧活跃条目 created_at=100, now=1000 → 900s = 0.25h
    assert s["oldest_span_hours"] == pytest.approx(0.25, abs=1e-3)


def test_multi_npc_aggregation(tmp_path):
    a = _load(_npc(tmp_path, "a"), [
        {"id": "1", "content": "x" * 100, "importance": 5, "category": "general",
         "created_at": 1.0},
        {"id": "2", "content": "y" * 50, "importance": 5, "category": "general",
         "created_at": 2.0},
    ])
    b = _load(_npc(tmp_path, "b"), [
        {"id": "1", "content": "z" * 30, "importance": 5, "category": "general",
         "created_at": 3.0},
    ])
    r = mr.survey({"a": a, "b": b})
    g = r["global"]
    assert g["npc_count"] == 2
    assert g["total_entries"] == 3
    assert g["total_tokens"] == 180                 # 100+50+30
    # tidy_ranking 含两个 NPC, 按 token 降序
    assert [x["actor_id"] for x in g["tidy_ranking"]] == ["a", "b"]


# ── 重复检测(count 字段也要识别) ──────────────────────────────

def test_duplicate_groups_by_separate_entries(tmp_path):
    npc = _load(_npc(tmp_path), [
        {"id": "1", "content": "去森林", "category": "general", "created_at": 1.0},
        {"id": "2", "content": "去森林", "category": "general", "created_at": 2.0},
        {"id": "3", "content": "独一条", "category": "general", "created_at": 3.0},
    ])
    s = mr.survey({"a1": npc})["per_npc"]["a1"]
    assert s["duplicate_group_count"] == 1
    g = s["duplicate_groups"][0]
    assert g["content"] == "去森林" and g["times"] == 2 and g["entries"] == 2


def test_duplicate_via_count_field(tmp_path):
    """开启去重后同文合并成 1 条 entry 但 count=3 → 仍应判为重复(≥2 次)。"""
    npc = _load(_npc(tmp_path), [
        {"id": "1", "content": "日常打招呼", "category": "general",
         "created_at": 1.0, "count": 3},
    ])
    s = mr.survey({"a1": npc})["per_npc"]["a1"]
    assert s["duplicate_group_count"] == 1
    assert s["duplicate_groups"][0]["times"] == 3


def test_global_top_duplicates_aggregates_across_npcs(tmp_path):
    a = _load(_npc(tmp_path, "a"), [
        {"id": "1", "content": "重复句", "category": "general", "created_at": 1.0},
        {"id": "2", "content": "重复句", "category": "general", "created_at": 2.0},
    ])
    b = _load(_npc(tmp_path, "b"), [
        {"id": "1", "content": "重复句", "category": "general", "created_at": 3.0},
        {"id": "2", "content": "重复句", "category": "general", "created_at": 4.0},
        {"id": "3", "content": "重复句", "category": "general", "created_at": 5.0},
    ])
    g = mr.survey({"a": a, "b": b})["global"]
    assert g["top_duplicates"], "应有全库重复榜"
    top = g["top_duplicates"][0]
    assert top["content"] == "重复句"
    assert top["times"] == 5                        # 2 + 3 跨 NPC 合计
    assert top["npc_count"] == 2


# ── 未分型 / 三分类 ───────────────────────────────────────────

def test_typed_distribution_and_untyped(tmp_path):
    npc = _load(_npc(tmp_path), [
        {"id": "1", "content": "c1", "category": "general", "mtype": "persona",
         "created_at": 1.0},
        {"id": "2", "content": "c2", "category": "general", "mtype": "episodic",
         "created_at": 2.0},
        {"id": "3", "content": "c3", "category": "general", "mtype": "instruction",
         "created_at": 3.0},
        {"id": "4", "content": "c4", "category": "general", "created_at": 4.0},  # 未分型
        {"id": "5", "content": "c5", "category": "general", "created_at": 5.0},  # 未分型
    ])
    s = mr.survey({"a1": npc})["per_npc"]["a1"]
    assert s["mtype_distribution"] == {"persona": 1, "episodic": 1, "instruction": 1}
    assert s["untyped"] == 2
    assert s["untyped_ratio"] == pytest.approx(2 / 5)


# ── 超阈值判定(复用 should_emergency 口径) ────────────────────

def test_over_threshold_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("NPC_MEMORY_TOKEN_MAX", "500")
    big = _load(_npc(tmp_path, "big"), [
        {"id": "1", "content": "x" * 600, "category": "general", "created_at": 1.0},
    ])
    small = _load(_npc(tmp_path, "small"), [
        {"id": "1", "content": "y" * 100, "category": "general", "created_at": 1.0},
    ])
    r = mr.survey({"big": big, "small": small})
    assert r["per_npc"]["big"]["over_threshold"] is True
    assert r["per_npc"]["small"]["over_threshold"] is False
    assert r["global"]["over_threshold_count"] == 1
    # 最该整理榜: 超阈的排在前
    assert r["global"]["tidy_ranking"][0]["actor_id"] == "big"


def test_threshold_limit_reflected_globally(tmp_path, monkeypatch):
    monkeypatch.setenv("NPC_MEMORY_TOKEN_MAX", "1234")
    r = mr.survey({})
    assert r["global"]["threshold_limit"] == 1234


# ── 流民单列跳过 ─────────────────────────────────────────────

def test_ephemeral_skipped_and_listed(tmp_path):
    normal = _load(_npc(tmp_path, "n1"), [
        {"id": "1", "content": "常驻记忆", "category": "general", "created_at": 1.0},
    ])
    ghost = _load(_npc(tmp_path, "g1", ephemeral=True), [
        {"id": "1", "content": "流民记忆", "category": "general", "created_at": 1.0},
    ])
    r = mr.survey({"n1": normal, "g1": ghost})
    assert "g1" not in r["per_npc"]                 # 不进 per_npc 统计
    assert "n1" in r["per_npc"]
    g = r["global"]
    assert g["npc_count"] == 1                       # 只数常驻
    assert g["ephemeral_count"] == 1
    assert g["ephemeral_ids"] == ["g1"]              # 明示被跳过
    assert g["total_entries"] == 1                   # 流民条目不计入体量


# ── CLI `--json` 纯度(子进程 + json.loads 必过) ───────────────

def test_cli_json_is_pure_json_via_subprocess(tmp_path):
    """真正的 CI 契约: stdout 从头到尾能被 json.loads(不能混日志)。"""
    # 造两张记忆卡落盘, 让 CLI 从 --store 加载
    a = _npc(tmp_path, "a")
    a.memory.load([
        {"id": "1", "content": "x" * 600, "category": "general", "created_at": 1.0},
        {"id": "2", "content": "去森林", "category": "general", "created_at": 2.0},
        {"id": "3", "content": "去森林", "category": "general", "created_at": 3.0},
    ])
    a.save()
    b = _npc(tmp_path, "b")
    b.memory.load([
        {"id": "1", "content": "y" * 50, "category": "general", "created_at": 4.0},
    ])
    b.save()

    proc = subprocess.run(
        [sys.executable, "-m", "npc.memory_report", "--json",
         "--store", str(tmp_path), "--top", "5"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=180,
        env={**os.environ, "NPC_MEMORY_TOKEN_MAX": "500"})
    assert proc.returncode == 0, proc.stderr[-1000:]
    parsed = json.loads(proc.stdout)                # ← 混进一行日志这里就炸
    assert parsed["global"]["npc_count"] == 2
    assert parsed["global"]["total_tokens"] == 656     # 600 + "去森林"×3×2 + 50
    assert parsed["per_npc"]["a"]["over_threshold"] is True
    assert parsed["global"]["top_duplicates"][0]["content"] == "去森林"


def test_cli_json_empty_store_is_valid_json(tmp_path):
    """空 store(无卡)也应输出合法(纯)JSON, 不退非零、不报错。"""
    proc = subprocess.run(
        [sys.executable, "-m", "npc.memory_report", "--json",
         "--store", str(tmp_path)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120)
    assert proc.returncode == 0, proc.stderr[-1000:]
    parsed = json.loads(proc.stdout)
    assert parsed["global"]["npc_count"] == 0
