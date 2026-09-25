"""tools.py 测试 —— 含白名单红线：未声明的动作不会成为工具。"""
from __future__ import annotations

import pytest

from core.schema import ToolResultStatus

import memory
import tools


# 协议.md §1 的三动作声明
CAPS = [
    {"name": "cook", "desc": "用厨房做饭", "params": {"dish": "菜名(字符串)"}},
    {"name": "goto", "desc": "走到某地", "params": {"place": "地点名(字符串)"}},
    {"name": "chat", "desc": "主动找某人说话", "params": {"target": "对象名", "topic": "话题"}},
]


def _defs_by_name(defs):
    return {d["function"]["name"]: d for d in defs}


# ── schema 形态 ──────────────────────────────────────────

def test_memory_tool_schemas():
    rem = tools.REMEMBER_TOOL.to_openai_function()
    assert rem["type"] == "function"
    assert rem["function"]["name"] == "remember"
    assert rem["function"]["parameters"]["required"] == ["content"]
    assert set(rem["function"]["parameters"]["properties"]) == {"content", "importance", "category"}

    rec = tools.RECALL_TOOL.to_openai_function()
    assert rec["function"]["name"] == "recall"
    assert rec["function"]["parameters"]["required"] == ["query"]


def test_action_tool_schema_from_declaration():
    schema = tools.action_tool_schema(CAPS[0])
    assert schema.name == "cook" and schema.description == "用厨房做饭"
    fn = schema.to_openai_function()["function"]
    assert fn["parameters"]["properties"]["dish"] == {
        "type": "string", "description": "菜名(字符串)"}
    assert fn["parameters"]["required"] == ["dish"]


def test_action_without_params():
    fn = tools.action_tool_schema({"name": "wait", "desc": "等一会"}).to_openai_function()["function"]
    assert fn["parameters"]["properties"] == {} and fn["parameters"]["required"] == []


def test_build_tool_definitions_includes_declared_actions():
    defs = _defs_by_name(tools.build_tool_definitions(CAPS))
    assert set(defs) == {"remember", "recall", "cook", "goto", "chat"}
    assert defs["chat"]["function"]["parameters"]["required"] == ["target", "topic"]


def test_whitelist_red_line():
    """未声明的动作不可能出现在工具定义里（§1 白名单即唯一闸门）。"""
    defs = _defs_by_name(tools.build_tool_definitions([CAPS[0]]))
    assert "cook" in defs
    assert "goto" not in defs and "chat" not in defs
    assert not tools.is_action_tool("goto", [CAPS[0]])
    assert tools.is_action_tool("cook", CAPS)


def test_offline_mod_gets_memory_tools_only():
    defs = _defs_by_name(tools.build_tool_definitions([]))
    assert set(defs) == {"remember", "recall"}
    assert set(_defs_by_name(tools.build_tool_definitions(None))) == {"remember", "recall"}


def test_reserved_names_not_shadowed():
    caps = [{"name": "remember", "desc": "假装是记忆工具", "params": {}}]
    assert tools.is_action_tool("remember", caps) is False
    assert set(_defs_by_name(tools.build_tool_definitions(caps))) == {"remember", "recall"}


# ── 执行 ─────────────────────────────────────────────────

def test_run_remember_writes_card(tmp_store):
    res = tools.run_tool("remember", {"content": "玩家爱吃面", "importance": 7}, "cang")

    assert res.status == ToolResultStatus.SUCCESS
    entries = memory.load_card("cang")
    assert [e["content"] for e in entries] == ["玩家爱吃面"]
    assert entries[0]["importance"] == 7


def test_run_remember_defaults_and_clamping(tmp_store):
    tools.run_tool("remember", {"content": "随手一提"}, "cang")
    tools.run_tool("remember", {"content": "超范围", "importance": 99}, "cang")

    entries = memory.load_card("cang")
    assert entries[0]["importance"] == memory.DEFAULT_IMPORTANCE
    assert entries[1]["importance"] == 9


def test_run_remember_rejects_empty_content(tmp_store):
    res = tools.run_tool("remember", {"content": "  "}, "cang")
    assert res.status == ToolResultStatus.ERROR
    assert memory.load_card("cang") == []


def test_run_recall_returns_verbatim_sorted(tmp_store):
    memory.set_synonyms(None)
    memory.add_entry("cang", "今天天气不错")
    memory.add_entry("cang", "锅里煮着面", importance=9)

    res = tools.run_tool("recall", {"query": "面"}, "cang")

    assert res.status == ToolResultStatus.SUCCESS
    # 逐字返回（§4.4），按相关度排序
    assert res.data == "- 锅里煮着面\n- 今天天气不错"


def test_run_recall_empty_memory(tmp_store):
    res = tools.run_tool("recall", {"query": "面"}, "cang")
    assert res.status == ToolResultStatus.SUCCESS
    assert res.data == "（没有想起相关的事）"


def test_run_recall_passes_synonyms(tmp_store):
    memory.add_entry("cang", "木材")
    memory.add_entry("cang", "石头")

    res = tools.run_tool("recall", {"query": "柴"}, "cang",
                         synonyms={"木材": ["木材", "柴"]})
    assert res.data.splitlines()[0] == "- 木材"      # 同义词命中把它排到最前


def test_action_tool_is_not_executed(tmp_store):
    """动作工具走提议通道：不写卡、不执行（协议.md §3）。"""
    res = tools.run_tool("cook", {"dish": "面"}, "cang", capabilities=CAPS)

    assert res.status == ToolResultStatus.REJECTED
    assert memory.load_card("cang") == []


def test_unknown_tool_is_error_not_raise(tmp_store):
    res = tools.run_tool("乱来的工具", {}, "cang", capabilities=CAPS)
    assert res.status == ToolResultStatus.ERROR
    assert "未知工具" in res.error


def test_undeclared_action_name_is_unknown(tmp_store):
    res = tools.run_tool("goto", {"place": "河边"}, "cang", capabilities=[CAPS[0]])
    assert res.status == ToolResultStatus.ERROR


@pytest.mark.parametrize("bad", [None, "字符串", 123])
def test_run_tool_tolerates_non_dict_args(tmp_store, bad):
    res = tools.run_tool("remember", bad, "cang")
    assert res.status == ToolResultStatus.ERROR
