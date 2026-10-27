"""T12 · scripts/verify_reasoning_replay.py 的离线测试。

验证脚本本身只在有 key 时由人运行；这里测「没有 key 时的提示」与三种历史的构造口径，
一律不发网络请求。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import verify_reasoning_replay as vrr      # noqa: E402


PREV = [
    {"role": "assistant", "content": "好的。", "reasoning_content": "先想想",
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "get_time", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "12:00"},
    {"role": "assistant", "content": "现在是 12:00。", "reasoning_content": "报时间"},
]
IN_TURN = [
    {"role": "assistant", "content": "", "reasoning_content": "再查一次",
     "tool_calls": [{"id": "call_2", "type": "function",
                     "function": {"name": "get_time", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_2", "content": "12:05"},
]


def test_build_histories_three_variants():
    """A 全留 / B 只去历史轮 / C 都去；两段分开返回，拼消息时中间要插用户那句。"""
    histories = vrr.build_histories(PREV, IN_TURN)

    assert set(histories) == {"A", "B", "C"}
    prev_a, in_a = histories["A"]
    prev_b, in_b = histories["B"]
    prev_c, in_c = histories["C"]

    assert [m.get("reasoning_content") for m in prev_a] == ["先想想", None, "报时间"]
    assert "reasoning_content" in in_a[0]

    assert ["reasoning_content" in m for m in prev_b] == [False, False, False]
    assert "reasoning_content" in in_b[0]                       # 本回合仍带

    assert all("reasoning_content" not in m for m in prev_c + in_c)

    # 入参没被改动（脚本跑完原历史仍是原样）
    assert PREV[0]["reasoning_content"] == "先想想"
    assert IN_TURN[0]["reasoning_content"] == "再查一次"


def test_strip_reasoning_keeps_other_fields():
    """去思考只摘 reasoning_content，tool_calls / role / content 都留着。"""
    stripped = vrr.strip_reasoning(PREV)

    assert stripped[0]["tool_calls"] == PREV[0]["tool_calls"]
    assert stripped[0]["content"] == PREV[0]["content"]
    assert stripped[1] == PREV[1]                              # tool 消息原样
    assert "reasoning_content" not in stripped[0]


def test_assistant_message_from_response():
    """响应 → 回传消息：认 reasoning_content / reasoning，tool_calls 照搬。"""
    data = {"choices": [{"message": {
        "content": "好", "reasoning_content": "想一想",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"}}]}}]}
    msg = vrr.assistant_message(data)

    assert msg["role"] == "assistant" and msg["content"] == "好"
    assert msg["reasoning_content"] == "想一想"
    assert msg["tool_calls"][0]["id"] == "c1"

    # 没有思考字段就不塞（别给不认这个字段的端点发空串）
    assert "reasoning_content" not in vrr.assistant_message(
        {"choices": [{"message": {"content": "嗯"}}]})
    # 坏响应不炸
    assert vrr.assistant_message({}) == {"role": "assistant", "content": ""}


def test_verdict_paths():
    """结论文本：B 通过 / 全通过 / 只有 A 可用 / 没拿到思考时不可判定。"""
    ok = {"status": 200}
    bad = {"status": 400}

    assert "B 通过" in vrr._verdict({"A": ok, "B": ok, "C": bad}, True)
    assert "都通过" in vrr._verdict({"A": ok, "B": ok, "C": ok}, True)
    assert "只有现状" in vrr._verdict({"A": ok, "B": bad, "C": bad}, True)
    assert "不可判定" in vrr._verdict({"A": ok, "B": ok, "C": bad}, False)


def test_endpoint_and_thinking_body():
    """端点拼接不出现双斜杠；未指定思考档位 / 不支思考参数时不发这些字段。"""
    assert vrr.endpoint_url("https://api.deepseek.com") == \
        "https://api.deepseek.com/chat/completions"
    assert vrr.endpoint_url("https://x/v1/") == "https://x/v1/chat/completions"

    settings = {"key": "sk-test", "model": "m", "base_url": "https://x/v1",
                "reasoning_effort": "", "thinking_unsupported": False}
    assert vrr.thinking_body(settings) == {}
    assert vrr.thinking_body({**settings, "thinking_unsupported": True,
                              "reasoning_effort": "high"}) == {}


def test_main_without_key_prints_hint_and_exits(monkeypatch, capsys):
    """没有 key：说清该配什么后退出（退出码 2），不发任何请求。"""
    if (ROOT / "api_key.txt").exists():
        pytest.skip("仓库根存在 api_key.txt，无法构造「没有 key」的场景")
    for name in ("NPC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    code = vrr.main(["--out", str(ROOT / "out" / "_never_written.json")])
    printed = capsys.readouterr().out

    assert code == 2
    assert "未配置完整" in printed
    assert "NPC_API_KEY" in printed
    assert not (ROOT / "out" / "_never_written.json").exists()
