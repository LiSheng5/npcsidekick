"""T4 · remember 静默收尾（NPC_SILENT_REMEMBER）。

收尾规则（`turn._stream_llm_rounds` 每轮工具执行完判断）：
  · 有非静默调用（recall / 动作）→ 照常继续；
  · 有 bad_args（模型重发才能修好）→ 继续；
  · 整个回合到现在一句台词都没出 → 继续；
  · 否则（只有 remember 且台词已出）→ 提前收尾（trace.ended_early=true）。

验收场景：说话 + remember → 1 轮；只有 remember 无台词 → 2 轮且第二轮无工具；
remember + recall → 照常继续。开关关时保持旧节奏（工具轮之后总是再拿一轮台词）。
"""
from __future__ import annotations

import json

import chatlog
import memory
from conftest import kinds, text_of


def read_traces(path) -> list:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_speak_plus_remember_finishes_in_one_round(tmp_store, write_persona,
                                                   make_app_client, make_provider,
                                                   monkeypatch, tmp_path):
    """说话 + remember 同轮：1 轮收尾（ended_early），remember 照常落卡，思考与台词配套。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("NPC_SILENT_REMEMBER", "1")
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "行啊，记住了。", "reasoning": "玩家说了件要紧事，顺手记下。",
         "tool_calls": [{"name": "remember",
                         "arguments": {"content": "玩家爱吃面", "importance": 7}}]},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "记住：我爱吃面"})

    assert text_of(res) == "行啊，记住了。"
    assert kinds(res)[-1] == "done"
    assert len(provider.stream_calls) == 1             # 就一轮，不再逼一轮台词
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "玩家爱吃面"]                                  # 静默调用照常执行
    turns = chatlog.load_turns("cang", store_dir=tmp_store)
    assert turns[-1]["reasoning"] == "玩家说了件要紧事，顺手记下。"   # 收尾那轮的思考
    trace = read_traces(trace_file)[0]
    assert trace["rounds"] == 1
    assert trace["ended_early"] is True
    assert trace["no_dialogue"] is False


def test_remember_only_forces_text_round_without_tools(tmp_store, write_persona,
                                                       make_app_client, make_provider,
                                                       monkeypatch):
    """只有 remember 无台词：继续，但下一轮不给工具（防连环调用），2 轮收工。"""
    monkeypatch.setenv("NPC_SILENT_REMEMBER", "1")
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "remember", "arguments": {"content": "玩家爱吃面"}}]},
        {"text": "记下了。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "记住：我爱吃面"})

    assert text_of(res) == "记下了。"
    assert len(provider.stream_calls) == 2
    assert provider.stream_calls[-1]["tools"] is None   # 第二轮无工具
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == ["玩家爱吃面"]


def test_remember_plus_recall_continues_as_usual(tmp_store, write_persona,
                                                 make_app_client, make_provider,
                                                 monkeypatch):
    """remember + recall 同轮：有非静默调用 → 照常继续（第二轮仍带工具），不提前收尾。"""
    monkeypatch.setenv("NPC_SILENT_REMEMBER", "1")
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "嗯，", "tool_calls": [
            {"name": "remember", "arguments": {"content": "玩家爱吃面"}},
            {"name": "recall", "arguments": {"query": "面"}}]},
        {"text": "想起来了，你说过。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "我上次说爱吃啥"})

    assert text_of(res) == "嗯，想起来了，你说过。"
    assert len(provider.stream_calls) == 2
    assert provider.stream_calls[-1]["tools"]          # 照常继续：工具还在


def test_later_silent_call_also_ends_early(tmp_store, write_persona,
                                           make_app_client, make_provider,
                                           monkeypatch):
    """前面轮次已出台词，后面只剩 remember：也当场收尾（台词义务已完成）。"""
    monkeypatch.setenv("NPC_SILENT_REMEMBER", "1")
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "我去看看。", "tool_calls": [{"name": "recall", "arguments": {"query": "面"}}]},
        {"text": "顺便记一笔。", "tool_calls": [
            {"name": "remember", "arguments": {"content": "玩家要一碗面"}}]},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "煮碗面"})

    assert text_of(res) == "我去看看。顺便记一笔。"
    assert len(provider.stream_calls) == 2             # recall 轮 + 静默收尾轮
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "玩家要一碗面"]


def test_failed_silent_call_only_logged_not_retried(tmp_store, write_persona,
                                                    make_app_client, make_provider,
                                                    monkeypatch, tmp_path):
    """失败的静默调用只落日志和 trace 状态：有台词照样提前收尾，不写卡、不追加轮次。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("NPC_SILENT_REMEMBER", "1")
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "说完了。", "tool_calls": [{"name": "remember", "arguments": {"content": ""}}]},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "随便说点"})

    assert text_of(res) == "说完了。"
    assert len(provider.stream_calls) == 1
    assert memory.load_card("cang", store_dir=tmp_store) == []
    trace = read_traces(trace_file)[0]
    assert trace["ended_early"] is True
    assert trace["tools"][0]["status"] == "error"      # 如实记失败，只落日志和 trace
    assert trace["tools"][0]["ok"] is False


def test_switch_off_keeps_old_rounds(tmp_store, write_persona,
                                     make_app_client, make_provider,
                                     monkeypatch):
    """开关关（NPC_SILENT_REMEMBER=0）：说话 + remember 仍是 2 轮（旧节奏），不记 ended_early。"""
    monkeypatch.setenv("NPC_SILENT_REMEMBER", "0")
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "行啊，记住了。", "tool_calls": [
            {"name": "remember", "arguments": {"content": "玩家爱吃面"}}]},
        {"text": "还有事吗。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "记住：我爱吃面"})

    assert text_of(res) == "行啊，记住了。还有事吗。"
    assert len(provider.stream_calls) == 2             # 旧节奏：工具轮后再拿一轮台词
    assert provider.stream_calls[-1]["tools"]          # 第二轮照常带工具
