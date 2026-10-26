"""tools.py 测试 —— 含白名单红线：未声明的动作不会成为工具。"""
from __future__ import annotations

import pytest

from core.schema import ToolResultStatus

import memory
import tools
from conftest import ACTIONS


def _defs_by_name(defs):
    return {d["function"]["name"]: d for d in defs}


# ── schema 形态 ──────────────────────────────────────────

def test_memory_tool_schemas():
    rem = tools.REMEMBER_TOOL.to_openai_function()
    assert rem["type"] == "function"
    assert rem["function"]["name"] == "remember"
    assert rem["function"]["parameters"]["required"] == ["content"]
    assert set(rem["function"]["parameters"]["properties"]) == {
        "content", "importance", "category", "replaces"}
    assert rem["function"]["parameters"]["properties"]["replaces"]["type"] == "string"
    assert "replaces" in rem["function"]["description"]

    rec = tools.RECALL_TOOL.to_openai_function()
    assert rec["function"]["name"] == "recall"
    assert rec["function"]["parameters"]["required"] == ["query"]


def test_action_tool_schema_from_declaration():
    schema = tools.action_tool_schema(ACTIONS[0])
    assert schema.name == "cook" and schema.description == "用厨房做饭"
    fn = schema.to_openai_function()["function"]
    assert fn["parameters"]["properties"]["dish"] == {
        "type": "string", "description": "菜名(字符串)"}
    assert fn["parameters"]["required"] == ["dish"]


def test_action_without_params():
    fn = tools.action_tool_schema({"name": "wait", "desc": "等一会"}).to_openai_function()["function"]
    assert fn["parameters"]["properties"] == {} and fn["parameters"]["required"] == []


# ── T6：对象形态 params（type / enum / desc / optional）─────

def test_action_tool_schema_object_params():
    """对象写法生效：type / enum 进 schema，optional 的不进 required；老写法保持不变。"""
    schema = tools.action_tool_schema({
        "name": "give", "desc": "给钱",
        "params": {
            "amount": {"type": "integer", "desc": "金额"},
            "kind": {"type": "string", "enum": ["coin", "gem"]},
            "reason": {"type": "string", "desc": "缘由", "optional": True},
        }})
    fn = schema.to_openai_function()["function"]
    props = fn["parameters"]["properties"]
    assert props["amount"] == {"type": "integer", "description": "金额"}
    assert props["kind"] == {"type": "string", "enum": ["coin", "gem"]}
    assert props["reason"] == {"type": "string", "description": "缘由"}
    assert fn["parameters"]["required"] == ["amount", "kind"]      # optional 不进 required
    assert "additionalProperties" not in fn["parameters"]           # 不加，个别网关不认


def test_action_tool_schema_old_string_params_unchanged():
    fn = tools.action_tool_schema(ACTIONS[0]).to_openai_function()["function"]
    assert fn["parameters"]["properties"]["dish"] == {
        "type": "string", "description": "菜名(字符串)"}
    assert fn["parameters"]["required"] == ["dish"]                # 老写法 = 必填 string


def test_param_spec_error_shape():
    """报到端点的形态校验：字符串/合法对象放行，其余400。"""
    assert tools.param_spec_error("菜名") is None
    assert tools.param_spec_error({"type": "integer", "desc": "x", "optional": True}) is None
    assert tools.param_spec_error({"type": "STRING", "enum": ["a"]}) is None
    assert tools.param_spec_error(123) is not None
    assert tools.param_spec_error(None) is not None
    assert tools.param_spec_error({"type": "money"}) is not None
    assert tools.param_spec_error({"enum": "不是数组"}) is not None
    assert tools.param_spec_error({"desc": 5}) is not None
    assert tools.param_spec_error({"optional": "yes"}) is not None


def test_build_tool_definitions_sorted():
    """remember / recall 恒在前，动作按名排序（前缀稳定）。"""
    names = [d["function"]["name"] for d in tools.build_tool_definitions(ACTIONS)]
    assert names == ["remember", "recall", "chat", "cook", "goto"]


# ── T6：validate_action_args（D9 口径）─────────────────────

DECL = {"name": "give", "desc": "给钱", "params": {
    "amount": {"type": "integer", "desc": "金额"},
    "kind": {"type": "string", "enum": ["coin", "gem"], "optional": True},
    "note": "备注（老写法 = 必填 string）",
}}


def test_validate_action_args_coerces_lossless_strings():
    """integer / number / boolean 接受可无损转换的字符串并转型。"""
    cleaned, err = tools.validate_action_args(DECL, {"amount": "3", "note": "辛苦", "kind": "coin"})
    assert err is None
    assert cleaned == {"amount": 3, "kind": "coin", "note": "辛苦"}

    decl = {"name": "f", "desc": "", "params": {
        "flag": {"type": "boolean"}, "ratio": {"type": "number"}, "n": {"type": "integer"}}}
    cleaned, err = tools.validate_action_args(decl, {"flag": "true", "ratio": "0.5", "n": "2"})
    assert err is None
    assert cleaned == {"flag": True, "ratio": 0.5, "n": 2}


def test_validate_action_args_missing_required():
    _, err = tools.validate_action_args(DECL, {"amount": 3})
    assert err and "缺少必填参数 note" in err


def test_validate_action_args_bad_type():
    _, err = tools.validate_action_args(DECL, {"amount": "三", "note": "x"})
    assert err and "amount" in err
    _, err = tools.validate_action_args(DECL, {"amount": 3, "note": 5})
    assert err and "note" in err


def test_validate_action_args_enum():
    _, err = tools.validate_action_args(DECL, {"amount": 3, "note": "x", "kind": "元宝"})
    assert err and "kind" in err and "允许的取值" in err


def test_validate_action_args_optional_ok_and_extra_dropped():
    cleaned, err = tools.validate_action_args(DECL, {"amount": 3, "note": "x", "没声明的": 1})
    assert err is None
    assert cleaned == {"amount": 3, "note": "x"}       # optional 缺席合法；多余参数丢弃不拒绝


def test_build_tool_definitions_includes_declared_actions():
    defs = _defs_by_name(tools.build_tool_definitions(ACTIONS))
    assert set(defs) == {"remember", "recall", "cook", "goto", "chat"}
    assert defs["chat"]["function"]["parameters"]["required"] == ["target", "topic"]


def test_whitelist_red_line():
    """未声明的动作不可能出现在工具定义里（白名单是唯一闸门）。"""
    defs = _defs_by_name(tools.build_tool_definitions([ACTIONS[0]]))
    assert "cook" in defs
    assert "goto" not in defs and "chat" not in defs
    assert not tools.is_action_tool("goto", [ACTIONS[0]])
    assert tools.is_action_tool("cook", ACTIONS)


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
    res = tools.run_tool("remember", {"content": "玩家爱吃面", "importance": 7}, "cang",
                         store_dir=tmp_store)

    assert res.status == ToolResultStatus.SUCCESS
    assert res.data == "已记住（重要性 7）"                 # 新增：旧文案
    entries = memory.load_card("cang", store_dir=tmp_store)
    assert [e["content"] for e in entries] == ["玩家爱吃面"]


def test_run_remember_defaults_and_clamping(tmp_store):
    r1 = tools.run_tool("remember", {"content": "随手一提"}, "cang", store_dir=tmp_store)
    r2 = tools.run_tool("remember", {"content": "超范围", "importance": 99}, "cang",
                        store_dir=tmp_store)

    assert r1.data == "已记住（重要性 5）"
    assert r2.data == "已记住（重要性 9）"


def test_run_remember_replaces_updates_old(tmp_store):
    old = memory.add_entry("cang", "玩家爱吃面", importance=7, store_dir=tmp_store)

    res = tools.run_tool("remember", {"content": "玩家喜欢面条",
                                      "replaces": old["id"][:6]}, "cang",
                         store_dir=tmp_store)

    assert res.status == ToolResultStatus.SUCCESS
    assert res.data == "已更新旧记忆"
    entries = memory.load_card("cang", store_dir=tmp_store)
    assert [e["content"] for e in entries] == ["玩家喜欢面条"]   # 更新，不是新增


def test_run_remember_replaces_unresolved_falls_back(tmp_store):
    res = tools.run_tool("remember", {"content": "玩家喜欢面条",
                                      "replaces": "zzzzzz"}, "cang",
                         store_dir=tmp_store)

    assert res.status == ToolResultStatus.SUCCESS
    assert res.data == "没找到要更新的旧记忆，已当新记忆记下（重要性 5）"
    assert len(memory.load_card("cang", store_dir=tmp_store)) == 1


def test_run_remember_rejects_empty_content(tmp_store):
    res = tools.run_tool("remember", {"content": "  "}, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR
    assert memory.load_card("cang", store_dir=tmp_store) == []


def test_run_recall_returns_verbatim_sorted(tmp_store):
    memory.add_entry("cang", "今天天气不错", store_dir=tmp_store)
    e = memory.add_entry("cang", "锅里煮着面", importance=9, store_dir=tmp_store)

    res = tools.run_tool("recall", {"query": "面"}, "cang", store_dir=tmp_store)

    assert res.status == ToolResultStatus.SUCCESS
    # 逐字返回（回忆结果照原文给模型），带 6 位短编号；不相关的不返回
    assert res.data == f"- [{e['id'][:6]}] 锅里煮着面"


def test_run_recall_empty_memory(tmp_store):
    res = tools.run_tool("recall", {"query": "面"}, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.SUCCESS
    assert res.data == "（没有想起相关的事）"


def test_run_recall_passes_synonyms(tmp_store):
    e = memory.add_entry("cang", "木材", store_dir=tmp_store)
    memory.add_entry("cang", "石头", store_dir=tmp_store)

    res = tools.run_tool("recall", {"query": "柴"}, "cang",
                         synonyms={"木材": ["木材", "柴"]}, store_dir=tmp_store)
    assert res.data == f"- [{e['id'][:6]}] 木材"     # 同义词命中召回；石头不相关不返回


def test_action_tool_is_not_executed(tmp_store):
    """动作工具不在 tools 里执行 —— 由 server 转发 mod 的 /execute（协议 §3）。"""
    res = tools.run_tool("cook", {"dish": "面"}, "cang", capabilities=ACTIONS,
                         store_dir=tmp_store)

    assert res.status == ToolResultStatus.REJECTED
    assert memory.load_card("cang", store_dir=tmp_store) == []


def test_unknown_tool_is_error_not_raise(tmp_store):
    res = tools.run_tool("乱来的工具", {}, "cang", capabilities=ACTIONS,
                         store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR
    assert "未知工具" in res.error


def test_undeclared_action_name_is_unknown(tmp_store):
    res = tools.run_tool("goto", {"place": "河边"}, "cang", capabilities=[ACTIONS[0]],
                         store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR


@pytest.mark.parametrize("bad", [None, "字符串", 123])
def test_run_tool_tolerates_non_dict_args(tmp_store, bad):
    res = tools.run_tool("remember", bad, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR
