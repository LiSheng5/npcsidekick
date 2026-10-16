"""context.assemble 测试 —— 任务 2：抽出上下文装配（不改行为）。

验收口径：对同一组输入，assemble 的 messages 与旧逻辑（stream_turn 原先那几行）
拼出的完全一致 —— 期望值在测试里手工构造，不复刻 assemble 的实现。
"""
from __future__ import annotations

from core.personas import build_system_prompt
import chatlog
import context


PERSONA = {
    "id": "cang", "name": "苍", "identity": "部落的老猎手",
    "personality": "寡言直接", "speech_style": "短句，不废话",
    "voice_samples": ["别追跑得快的。"], "taboos": ["烧湿柴"],
}

HISTORY = [
    {"role": "assistant", "content": "（此前对话摘要，供你参考）玩家爱吃面。"},
    {"role": "user", "content": "我上次说爱吃啥"},
    {"role": "assistant", "content": "你上次说过爱吃面。"},
]

MESSAGE = "那再做一碗"

OBSERVATION = {"summary": "玩家在厨房，刚下班", "nearby": ["玩家", "冰箱"]}


def test_assemble_messages_match_old_logic():
    """期望值手工构造：system = build_system_prompt(人设+观测) → 历史 → user 这句话。"""
    expected_system = build_system_prompt(PERSONA, OBSERVATION)
    expected = [{"role": "system", "content": expected_system}]
    expected.extend(HISTORY)
    expected.append({"role": "user", "content": MESSAGE})

    got = context.assemble(PERSONA, OBSERVATION, HISTORY, MESSAGE)

    assert got.messages == expected
    assert got.messages[0]["role"] == "system"
    assert "部落的老猎手" in got.messages[0]["content"]     # 人设进了 system
    assert "玩家在厨房" in got.messages[0]["content"]       # 观测仍进 system（任务 3 才挪）
    assert got.messages[-1] == {"role": "user", "content": MESSAGE}


def test_assemble_messages_match_old_logic_without_observation():
    expected = [{"role": "system", "content": build_system_prompt(PERSONA)}]
    expected.append({"role": "user", "content": MESSAGE})

    got = context.assemble(PERSONA, None, [], MESSAGE)

    assert got.messages == expected


def test_assemble_section_tokens():
    got = context.assemble(PERSONA, OBSERVATION, HISTORY, MESSAGE)

    assert set(got.section_tokens) == {"system", "history", "user"}
    assert got.section_tokens["system"] == chatlog.estimate_tokens(
        build_system_prompt(PERSONA, OBSERVATION))
    assert got.section_tokens["history"] == sum(
        chatlog.estimate_tokens(m["content"]) for m in HISTORY)
    assert got.section_tokens["user"] == chatlog.estimate_tokens(MESSAGE)


def test_assemble_is_pure_no_io(tmp_path, monkeypatch):
    """纯函数：不写任何文件、不读 store —— 跑完 tmp 目录里什么都没有。"""
    monkeypatch.chdir(tmp_path)
    got = context.assemble(PERSONA, OBSERVATION, HISTORY, MESSAGE)
    assert got.messages and got.section_tokens
    assert list(tmp_path.iterdir()) == []


def test_assemble_history_with_empty_content():
    """历史里混入空 content 不炸（真实摘要/工具消息可能为空串）。"""
    history = [{"role": "user", "content": ""}, {"role": "assistant", "content": "嗯"}]
    got = context.assemble(PERSONA, None, history, MESSAGE)
    assert [m["role"] for m in got.messages] == ["system", "user", "assistant", "user"]
    assert got.section_tokens["history"] == chatlog.estimate_tokens("嗯")
