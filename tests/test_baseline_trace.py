"""T11 · scripts/baseline_trace.py 的 stats 离线单测。

手造 trace JSONL 与 kinds.json，只验算数与分组口径 —— 不碰网络、不需要真实 key。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import baseline_trace as bt      # noqa: E402  脚本模块（import 时会带进 manual_checks）


def _tool(name: str, kind: str, status: str = "success") -> dict:
    return {"name": name, "kind": kind, "ok": status == "success", "status": status, "ms": 1.0}


def _rec(rounds: int, first: float, total: float, tokens: int, tools: list,
         ended_early: bool = False) -> dict:
    """一条够用的 turn_trace 记录（只留 stats 会读的字段）。"""
    return {"npc_id": "cang", "rounds": rounds, "first_token_ms": first, "total_ms": total,
            "tokens": {"total": tokens}, "tools": tools, "ended_early": ended_early,
            "llm_fallback": False, "no_dialogue": False, "summarized": False}


# 5 个回合：闲聊 / 记忆 / 动作（参数被拦） / 混合（做了动作） / 混合（没做动作）
RECORDS = [
    _rec(1, 800.0, 1500.0, 100, []),
    _rec(2, 900.0, 2000.0, 200, [_tool("remember", "memory")], ended_early=True),
    _rec(2, 1000.0, 3000.0, 300, [_tool("cook", "action", "invalid_args")]),
    _rec(3, 1100.0, 4000.0, 400, [_tool("remember", "memory"), _tool("cook", "action", "done")]),
    _rec(2, 1200.0, 2500.0, 500, [_tool("remember", "memory")]),
]
KINDS = ["chat", "remember", "action", "mixed", "mixed"]


def test_stats_without_kinds_keeps_plain_output():
    """没有 kinds.json：不出分组块（与 V3 基线一致），但照样给 invalid_args 数据。"""
    stats = bt.compute_stats(RECORDS)

    assert "kinds" not in stats
    assert stats["metrics"]["rounds"]["n"] == 5
    assert stats["tools_called"] == {"remember": 3, "cook": 2}
    # 2 次 action 调用里 1 次被参数校验拦下
    assert stats["invalid_args"] == {"count": 1, "action_calls": 2, "ratio": 0.5}


def test_kind_groups_metrics_and_mixed_action_lost():
    """按 kind 分组：回合数 / rounds 与耗时 p50、p95 / ended_early；mixed 组另给动作丢失。"""
    stats = bt.compute_stats(RECORDS, kinds=KINDS, kinds_source="kinds.json")
    groups = stats["kinds"]["groups"]

    assert stats["kinds"]["turns"] == 5 and stats["kinds"]["records"] == 5
    assert groups["chat"]["turns"] == 1
    assert groups["remember"]["ended_early"] == 1

    mixed = groups["mixed"]
    assert mixed["turns"] == 2
    assert mixed["rounds"] == {"p50": 2, "p95": 3}          # rounds = [3, 2] → 最近秩
    assert mixed["first_token_ms"] == {"p50": 1100.0, "p95": 1200.0}
    assert mixed["total_ms"] == {"p50": 2500.0, "p95": 4000.0}
    assert mixed["ended_early"] == 0
    assert mixed["action_missing"] == 1                      # 第 5 回合没有 action 记录
    assert "启发式" in mixed["action_missing_note"]           # 口径必须写明是启发式


def test_kind_groups_pad_missing_kinds_as_unknown():
    """kinds 比 trace 短时，多出来的回合记为 unknown（不静默丢样本）。"""
    stats = bt.compute_stats(RECORDS, kinds=["chat"])
    groups = stats["kinds"]["groups"]

    assert groups["chat"]["turns"] == 1
    assert groups["unknown"]["turns"] == 4


def test_kinds_roundtrip():
    """kinds_payload ↔ _read_kinds 互通；坏文件 → None。"""
    path = Path(bt.__file__).with_name("_kinds_test_tmp.json")
    payload = bt.kinds_payload(["chat", "mixed"])
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        assert bt._read_kinds(path) == ["chat", "mixed"]
        path.write_text('{"kinds": ["chat", "", 3]}', encoding="utf-8")
        assert bt._read_kinds(path) == ["chat", "unknown", "3"]   # 空项占位不挪对齐
        path.write_text("不是 JSON", encoding="utf-8")
        assert bt._read_kinds(path) is None
        path.write_text('{"kinds": []}', encoding="utf-8")
        assert bt._read_kinds(path) is None
    finally:
        path.unlink(missing_ok=True)


def _write_trace(dir_path: Path) -> Path:
    trace = dir_path / "trace.jsonl"
    trace.write_text("\n".join(json.dumps(rec, ensure_ascii=False) for rec in RECORDS) + "\n",
                     encoding="utf-8")
    return trace


def test_cmd_stats_reads_sibling_kinds_json(tmp_path, capsys):
    """stats 发现同目录 kinds.json → 控制台多出分组表与 mixed 动作丢失（标启发式）。"""
    trace = _write_trace(tmp_path)
    (tmp_path / "kinds.json").write_text(json.dumps(bt.kinds_payload(KINDS), ensure_ascii=False),
                                        encoding="utf-8")
    out = tmp_path / "stats.json"

    code = bt.cmd_stats(bt.build_parser().parse_args(["stats", str(trace), "--out", str(out)]))
    printed = capsys.readouterr().out

    assert code == 0
    assert "=== V3 基线统计（5 回合） ===" in printed
    assert "=== 按 kind 分组" in printed
    assert "mixed" in printed and "动作丢失: 1 条" in printed
    assert "启发式" in printed
    saved = json.loads(out.read_text("utf-8"))
    assert saved["kinds"]["groups"]["mixed"]["action_missing"] == 1


def test_cmd_stats_without_kinds_json_hides_groups(tmp_path, capsys):
    """没有 kinds.json：保持原有输出，不出现分组段。"""
    trace = _write_trace(tmp_path)

    code = bt.cmd_stats(bt.build_parser().parse_args(["stats", str(trace)]))
    printed = capsys.readouterr().out

    assert code == 0
    assert "=== V3 基线统计（5 回合） ===" in printed
    assert "按 kind 分组" not in printed
    assert "invalid_args   1 次 / 2 次 action 调用 = 50.00%" in printed


def test_cmd_stats_explicit_missing_kinds_fails(tmp_path, capsys):
    """--kinds 指到不存在的文件 → 明确报错退出（不静默当没有）。"""
    trace = _write_trace(tmp_path)

    code = bt.cmd_stats(bt.build_parser().parse_args(
        ["stats", str(trace), "--kinds", str(tmp_path / "nope.json")]))

    assert code == 1
    assert "找不到 kinds 文件" in capsys.readouterr().out


def test_env_pairs_and_snapshot(monkeypatch):
    """--env 透传：KEY=VALUE 解析；快照记下本次生效的开关（关/开两遍靠它认）。"""
    pairs = bt._parse_env_pairs(["NPC_SILENT_REMEMBER=1", "NPC_VALIDATE_ACTION_ARGS=0"])
    assert pairs == {"NPC_SILENT_REMEMBER": "1", "NPC_VALIDATE_ACTION_ARGS": "0"}

    with pytest.raises(SystemExit):
        bt._parse_env_pairs(["没有等号"])

    monkeypatch.setenv("NPC_SILENT_REMEMBER", "1")
    monkeypatch.delenv("NPC_VALIDATE_ACTION_ARGS", raising=False)
    snapshot = bt._env_snapshot(pairs)
    assert snapshot["NPC_SILENT_REMEMBER"] == "1"
    assert snapshot["NPC_VALIDATE_ACTION_ARGS"] == ""      # 没设 → 空串，键仍在
    assert "AGENT_MODEL" in snapshot                       # 模型名也记一份，便于对照两遍


def test_script_has_mixed_cases():
    """SCRIPT 里至少有 4 条 mixed（同一句既要记忆又要动作），第 4 节的判据才有样本。"""
    mixed = [item for item in bt.SCRIPT if item["kind"] == "mixed"]
    assert len(mixed) >= 4
    for item in mixed:
        assert "记" in item["message"]        # 记忆意图
    assert len({item["message"] for item in bt.SCRIPT}) == len(bt.SCRIPT)   # 没有重复句
