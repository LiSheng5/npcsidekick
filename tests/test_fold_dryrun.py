"""T13 · scripts/fold_dryrun.py 的离线测试：大存档干跑折叠，原目录一个字节都不能改。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import fold_dryrun                      # noqa: E402  脚本模块

import chatlog                          # noqa: E402


def _build_store(store: Path, turns: int, chars: int = 200) -> Path:
    """造一份"老存档"：每轮 200 字上下，够跨好几个分片。"""
    store.mkdir(parents=True, exist_ok=True)
    for i in range(turns):
        chatlog.append_turn("cang", "问" * chars + str(i), "答" * chars + str(i),
                            store_dir=store)
    return store


def _snapshot(store: Path) -> dict:
    """存档目录的字节快照（用于断言原目录没被改）。"""
    return {p.name: p.read_bytes() for p in sorted(store.iterdir()) if p.is_file()}


def test_dryrun_folds_everything_and_leaves_store_untouched(tmp_path, monkeypatch):
    """40 轮老存档：切片折叠到底（covered 追平 cutoff），传入的原目录一个字节不变。"""
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "3")
    store = _build_store(tmp_path / "store", turns=40)
    before = _snapshot(store)

    result = fold_dryrun.run(store, "cang", window=8000)

    assert result["window"] == 8000
    assert result["slice_cap"] == 2400                  # 窗口 × 0.3
    assert result["cutoff"] == 80 - 3 * 2               # 保留最近 3 轮
    assert result["backlog_turns"] == (80 - 6) // 2
    assert result["slices"] >= 2                        # 单片装不下，必须切
    assert result["max_slice_tokens"] is not None
    assert result["max_over_slice_cap"] is False        # 每片都不超上限
    assert result["covered_before"] == 0
    assert result["covered_after"] == result["cutoff"]
    assert result["covered_equals_cutoff"] is True
    assert result["summary_chars"] > 0

    assert _snapshot(store) == before                   # 原目录未被修改
    assert not (store / "cang_summary.json").exists()   # 摘要只落在临时副本里


def test_dryrun_without_backlog(tmp_path, monkeypatch):
    """只有 2 轮（都在保留窗口内）：没得压，分片数 0、covered 与 cutoff 视为相等。"""
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "3")
    store = _build_store(tmp_path / "store", turns=2)

    result = fold_dryrun.run(store, "cang", window=8000)

    assert result["slices"] == 0
    assert result["cutoff"] == 0
    assert result["backlog_turns"] == 0
    assert result["covered_equals_cutoff"] is True


def test_stub_llm_matches_maybe_summarize_signature(tmp_path, monkeypatch):
    """桩 LLM 的 chat 签名与真实 client 对齐（reasoning_effort 关键字），不需要联网。"""
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    store = _build_store(tmp_path / "store", turns=6)

    stub = fold_dryrun.StubLLM()
    summary = chatlog.maybe_summarize("cang", stub, store_dir=store)

    assert summary == fold_dryrun._STUB_SUMMARY
    assert stub.calls                                        # 至少切了一片
    assert all(call["reasoning_effort"] == "off" for call in stub.calls)
    assert all(call["input_tokens"] > 0 for call in stub.calls)


def test_main_reports_missing_store(tmp_path, capsys):
    """存档目录不存在 → 退出码 2，并说清是哪一步。"""
    code = fold_dryrun.main(["--store", str(tmp_path / "nope"), "--npc", "cang"])

    assert code == 2
    assert "存档目录不存在" in capsys.readouterr().out


def test_main_reports_missing_chatlog(tmp_path, capsys):
    """目录在、但没有这个 NPC 的聊天记录 → 退出码 2。"""
    store = tmp_path / "store"
    store.mkdir()

    code = fold_dryrun.main(["--store", str(store), "--npc", "cang"])

    assert code == 2
    assert "没有这个 NPC 的聊天记录" in capsys.readouterr().out


def test_main_rejects_illegal_npc_id(tmp_path, capsys):
    """npc_id 带路径穿越 → 退出码 2（走 core.paths 的校验）。"""
    store = _build_store(tmp_path / "store", turns=2)

    code = fold_dryrun.main(["--store", str(store), "--npc", "../evil"])

    assert code == 2
    assert "干跑中止" in capsys.readouterr().out


def test_main_success_writes_json(tmp_path, monkeypatch, capsys):
    """正常路径：退出码 0，--out 落一份同样的结果 JSON。"""
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "2")
    store = _build_store(tmp_path / "store", turns=12)
    out = tmp_path / "fold.json"

    code = fold_dryrun.main(["--store", str(store), "--npc", "cang",
                             "--window", "8000", "--out", str(out)])
    printed = capsys.readouterr().out

    assert code == 0
    assert "折叠干跑" in printed and "相等" in printed
    saved = json.loads(out.read_text("utf-8"))
    assert saved["covered_equals_cutoff"] is True
    assert saved["slice_cap"] == 2400
