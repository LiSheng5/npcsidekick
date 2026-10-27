"""T14 · scripts/prompt_compare.py 的离线测试（提示词构造 / 观测口径 / Markdown 渲染 / --dry）。

真跑要 key，由人执行；这里一律不发请求。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import prompt_compare as pc        # noqa: E402

PERSONA = {
    "identity": "部落的老猎手",
    "personality": "寡言直接",
    "speech_style": "短句，不废话",
    "voice_samples": ["嗯。"],
    "taboos": ["说谎"],
}


def test_legacy_prompt_keeps_persona_and_old_rules():
    """旧提示词 = 现提示词的「规矩:」之前那一段 + LEGACY_RULES（两版只差规矩）。"""
    current = pc.build_legacy_prompt(PERSONA)
    now = __import__("core.personas", fromlist=["build_system_prompt"]).build_system_prompt(PERSONA)

    head = now.partition("规矩:")[0]
    assert current.startswith(head)                     # 人设前缀照搬
    assert current.endswith(pc.LEGACY_RULES)
    assert "先查记忆（recall）" in current               # 旧规矩的措辞
    assert "台词要短" not in current                     # 新加的规矩不在旧版里
    assert "【相关记忆】" not in current
    assert "台词要短" in now and "【相关记忆】" in now
    assert len(now) > len(current)                      # 规矩确实变长了


def test_sentence_count_handles_cjk_and_ascii():
    assert pc.sentence_count("你好。我来了！真的吗？") == 3
    assert pc.sentence_count("嗯\n好。") == 2
    assert pc.sentence_count("   ") == 0


def _response(content: str, tool_names=()):
    calls = [SimpleNamespace(function=SimpleNamespace(name=name)) for name in tool_names]
    return SimpleNamespace(content=content, tool_calls=calls)


def test_measure_counts_chars_sentences_recall_and_lead():
    """四个观测量：字数 / 句数 / 是否 recall / 是否先说话再调工具。"""
    m = pc.measure(_response("行啊，我这就去。", ["cook"]))
    assert (m["chars"], m["sentences"]) == (len("行啊，我这就去。"), 1)
    assert m["recall"] is False
    assert m["spoke_before_tool"] is True

    # 只有工具调用、没有台词 → 不算「先说话」
    assert pc.measure(_response("", ["recall"]))["spoke_before_tool"] is False
    assert pc.measure(_response("", ["recall"]))["recall"] is True
    # 只有台词 → 没有工具调用
    quiet = pc.measure(_response("嗯。"))
    assert quiet["tool_calls"] == [] and quiet["spoke_before_tool"] is False


def test_aggregate_and_markdown_two_columns():
    """汇总口径与两栏并排的 Markdown（旧在左、现在右）。"""
    result = {
        "generated_at": "2026-10-27T10:00:00",
        "model": "fake-model",
        "inputs": ["在吗？"],
        "requests": 2,
        "prompt_chars": {"legacy": 400, "current": 500},
        "prompt_tokens": {"legacy": 300, "current": 380},
        "personas": [{
            "npc_id": "cang",
            "prompt_chars": {"legacy": 400, "current": 500},
            "prompt_tokens": {"legacy": 300, "current": 380},
            "rows": [{
                "message": "在吗？",
                "legacy": {"chars": 10, "sentences": 1, "tool_calls": [],
                           "recall": False, "spoke_before_tool": False},
                "current": {"chars": 6, "sentences": 1, "tool_calls": ["recall"],
                            "recall": True, "spoke_before_tool": True},
            }],
            "summary": {
                "legacy": pc._aggregate([{"chars": 10, "sentences": 1, "recall": False,
                                          "spoke_before_tool": False, "tool_calls": []}]),
                "current": pc._aggregate([{"chars": 6, "sentences": 1, "recall": True,
                                           "spoke_before_tool": True, "tool_calls": ["recall"]}]),
            },
        }],
    }

    markdown = pc.render_markdown(result)

    assert "| # | 玩家说 | 旧规矩 | 现规矩 |" in markdown
    assert "10 字 / 1 句 / recall 否 / 先说话 否" in markdown
    assert "6 字 / 1 句 / recall 是 / 先说话 是" in markdown
    assert "差 +100 字" in markdown and "差 +80" in markdown
    assert "| 调了 recall 的次数 | 0 | 1 |" in markdown


def test_dry_run_needs_no_key(capsys, monkeypatch):
    """--dry：只打印请求数与长度差，不发请求、不看 key。"""
    for name in ("NPC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    code = pc.main(["--dry"])
    printed = capsys.readouterr().out

    assert code == 0
    assert "将发出" in printed and "次请求" in printed
    assert "token" in printed and "差" in printed
    assert "去掉 --dry" in printed
