"""check_player_log 工具测试 —— 定义/保留字 + run_tool 执行（只读、可见性、参数）。"""
from __future__ import annotations

import time

import pytest

from core.schema import ToolResultStatus

import events
import tools
from conftest import ACTIONS


def _names(defs):
    return [d["function"]["name"] for d in defs]


# ── 工具定义 ────────────────────────────────────────────

def test_tool_schema_shape():
    fn = tools.PLAYER_LOG_TOOL.to_openai_function()["function"]
    assert fn["name"] == "check_player_log"
    assert fn["parameters"]["required"] == []            # 三个参数全可选
    assert set(fn["parameters"]["properties"]) == {"keyword", "minutes", "limit"}
    # 描述写清：什么时候查、别乱查、只能看到目击/公开、别声张
    desc = fn["description"]
    assert "玩家做过什么" in desc and "寒暄别查" in desc
    assert "亲眼看到" in desc and "公开" in desc
    assert "不要对玩家说" in desc


def test_build_tool_definitions_inserts_player_log_after_recall():
    """include_player_log=True 时插在 recall 之后、动作之前（顺序固定，利于前缀缓存）。"""
    names = _names(tools.build_tool_definitions(ACTIONS, include_player_log=True))
    assert names == ["remember", "recall", "check_player_log", "chat", "cook", "goto"]


def test_build_tool_definitions_default_excludes_player_log():
    assert "check_player_log" not in _names(tools.build_tool_definitions(ACTIONS))
    names = _names(tools.build_tool_definitions(None, include_player_log=True))
    assert names == ["remember", "recall", "check_player_log"]   # 无能力时也只插查询工具


def test_player_log_is_reserved_tool():
    """mod 声明同名动作不能遮蔽内置查询工具（保留词语义）。"""
    caps = [{"name": "check_player_log", "desc": "假装是查询工具", "params": {}}]
    assert tools.is_action_tool("check_player_log", caps) is False
    assert "check_player_log" not in _names(tools.build_tool_definitions(caps))
    assert tools.TOOL_CHECK_LOG in tools._RESERVED_TOOLS


def test_player_log_is_not_silent_tool():
    """占用工具轮数：不在静默工具表里（调完照常要多跑一轮拿台词）。"""
    assert tools.is_silent_tool("check_player_log") is False


# ── 执行（run_tool）────────────────────────────────────

def _seed(store_dir, now, count=3, prefix="k"):
    evs = []
    for i in range(count):
        evs.append({"kind": "by_player", "type": f"{prefix}{i}_started",
                    "label": f"行为{i}", "summary": f"玩家做了第{i}件事",
                    "merge_key": f"{prefix}{i}", "witnesses": ["cang"],
                    "location": "厨房" if i % 2 == 0 else None})
    events.record_events(events.validate_events(evs), store_dir=store_dir, now=now)


def test_run_check_player_log_defaults(tmp_store):
    now = time.time()
    _seed(tmp_store, now - 30)
    res = tools.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)

    assert res.status == ToolResultStatus.SUCCESS
    lines = res.data.split("\n")
    assert len(lines) == 3                               # 默认 limit=8，全在
    for ln in lines:
        assert ln.startswith("[") and "｜" in ln          # [编号6位] …｜…
    # last_ts 倒序（最新在前）
    assert "行为2" in lines[0] and "行为0" in lines[2]


def test_run_check_player_log_format(tmp_store):
    now = time.time()
    _seed(tmp_store, now - 30, count=2)
    res = tools.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)
    lines = res.data.split("\n")
    with_loc = [ln for ln in lines if ln.count("｜") == 3][0]   # [编号] 时间｜label｜summary｜地点
    without_loc = [ln for ln in lines if ln.count("｜") == 2][0]
    assert with_loc.endswith("｜厨房")                    # 有地点带最后一段
    assert not without_loc.endswith("｜")                # 无地点省略最后一段


def test_run_check_player_log_count_suffix(tmp_store):
    now = time.time()
    ev = {"kind": "by_player", "type": "cook_started", "label": "开始做饭",
          "summary": "玩家开始做饭", "merge_key": "c", "witnesses": ["cang"]}
    events.record_events(events.validate_events([ev]), store_dir=tmp_store, now=now - 60)
    events.record_events(events.validate_events([ev]), store_dir=tmp_store,
                         now=now - 30)                   # 同键合并 → count=2
    res = tools.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)
    assert "玩家开始做饭 ×2" in res.data


def test_run_check_player_log_keyword(tmp_store):
    now = time.time()
    _seed(tmp_store, now - 30, count=3)
    res = tools.run_tool("check_player_log", {"keyword": "第1件事"}, "cang",
                         store_dir=tmp_store)
    assert res.status == ToolResultStatus.SUCCESS
    assert res.data.count("\n") == 0 and "第1件事" in res.data

    # keyword 匹配 type / label 子串
    res = tools.run_tool("check_player_log", {"keyword": "k2_started"}, "cang",
                         store_dir=tmp_store)
    assert "第2件事" in res.data
    res = tools.run_tool("check_player_log", {"keyword": "行为1"}, "cang",
                         store_dir=tmp_store)
    assert "第1件事" in res.data


def test_run_check_player_log_minutes_window(tmp_store):
    now = time.time()
    _seed(tmp_store, now - 300, count=3)
    res = tools.run_tool("check_player_log", {"minutes": 1}, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.SUCCESS
    assert res.data == "没有符合条件的记录"              # 5 分钟前的事不在 1 分钟窗内


def test_run_check_player_log_limit_and_remaining(tmp_store):
    now = time.time()
    _seed(tmp_store, now - 30, count=12)
    res = tools.run_tool("check_player_log", {"limit": 5}, "cang", store_dir=tmp_store)
    lines = res.data.split("\n")
    assert len(lines) == 6                               # 5 行 + 剩余行
    assert lines[-1] == "还有 7 条没列出"

    # limit 上限 20：再来 13 条独立记录（合计 25），给 25 也只出 20 行
    _seed(tmp_store, now - 10, count=13, prefix="m")
    res = tools.run_tool("check_player_log", {"limit": 25}, "cang", store_dir=tmp_store)
    assert res.data.split("\n")[-1].startswith("还有 ")
    assert res.data.count("\n") == 20                    # 20 行 + 1 行剩余提示


def test_run_check_player_log_no_results(tmp_store):
    res = tools.run_tool("check_player_log", {"keyword": "不存在"}, "cang",
                         store_dir=tmp_store)
    assert res.status == ToolResultStatus.SUCCESS
    assert res.data == "没有符合条件的记录"


def test_run_check_player_log_visibility_filter(tmp_store):
    now = time.time()
    events.record_events(events.validate_events([
        {"kind": "by_player", "type": "mine", "summary": "我目击的", "merge_key": "m",
         "witnesses": ["cang"]},
        {"kind": "by_player", "type": "others", "summary": "别人目击的", "merge_key": "o",
         "witnesses": ["bai"]},
        {"kind": "by_player", "type": "open", "summary": "公开的", "merge_key": "p",
         "visibility": "public"},
    ]), store_dir=tmp_store, now=now)
    res = tools.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)
    assert "我目击的" in res.data and "公开的" in res.data
    assert "别人目击的" not in res.data                  # 非目击且非公开 → 查不到


def test_run_check_player_log_is_readonly(tmp_store):
    now = time.time()
    _seed(tmp_store, now - 30, count=2)
    before = events.list_events("by_player", store_dir=tmp_store)
    tools.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)
    after = events.list_events("by_player", store_dir=tmp_store)
    assert before == after                               # 只读，不改任何状态


def test_run_check_player_log_disabled_by_switch(tmp_store, monkeypatch):
    monkeypatch.setenv("NPC_PLAYER_LOG", "0")
    _seed(tmp_store, time.time() - 30, count=1)
    res = tools.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR
    assert "未开启" in res.error                         # 硬调用也绕不过开关


@pytest.mark.parametrize("args, expect", [
    ({"keyword": 123}, "keyword"),
    ({"minutes": 0}, "minutes"),
    ({"minutes": "abc"}, "minutes"),
    ({"limit": -1}, "limit"),
    ({"limit": 1.5}, "limit"),
])
def test_run_check_player_log_rejects_bad_args(tmp_store, args, expect):
    res = tools.run_tool("check_player_log", args, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR
    assert expect in res.error


def test_run_check_player_log_tolerates_non_dict_args(tmp_store):
    res = tools.run_tool("check_player_log", "不是对象", "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.SUCCESS       # 参数归一为空 → 全量查询
    assert res.data == "没有符合条件的记录"


def test_run_check_player_log_query_failure_is_clear_error(tmp_store, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(events, "visible_acts", boom)
    res = tools.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR
    assert "查询玩家行为记录失败" in res.error           # 明确错误，不静默
