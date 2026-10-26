"""turn.py 的回合 trace（turn_trace）测试。

每回合结束输出一条结构化日志 turn_trace（含降级回合）；NPC_TRACE_FILE 非空时
同一条记录以 JSONL 追加写入该文件。测试用 JSONL 落盘通道断言记录内容。
"""
from __future__ import annotations

import json

import chatlog
from conftest import CAPS, BoomProvider, FakeExecutor, frames, kinds, text_of


def read_traces(path) -> list[dict]:
    """读 NPC_TRACE_FILE 的 JSONL 记录。"""
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# turn_trace 必须携带的关键字段（超集口径：允许以后加新观测字段，不许挪走这些）
TRACE_KEYS = {"npc_id", "rounds", "llm_fallback", "summarized", "ended_early",
              "no_dialogue", "tools", "tokens", "summary_trigger", "memory_prefetch",
              "first_token_ms", "total_ms", "message_len", "reply_len"}


def test_turn_trace_two_tool_calls_records_rounds_and_tools(tmp_store, write_persona,
                                                            make_app_client, make_provider,
                                                            monkeypatch, tmp_path):
    """带 2 次工具调用的回合：rounds / tools 数量与实际一致，字段齐全，只记长度不记原文。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "行啊，", "tool_calls": [
            {"name": "remember", "arguments": {"content": "玩家爱吃面", "importance": 7}},
            {"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "面好了，趁热吃。"},
    ])
    executor = FakeExecutor()
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做碗面",
                                         "mod": "mygame"})

    assert kinds(res)[-1] == "done"
    traces = read_traces(trace_file)
    assert len(traces) == 1                       # 一条回合一条 trace
    trace = traces[0]
    assert TRACE_KEYS <= set(trace)          # 超集断言：加字段不红，挪字段才红
    assert trace["npc_id"] == "cang"
    assert trace["rounds"] == 2                   # 一轮工具 + 一轮收尾台词
    assert len(provider.stream_calls) == 2
    assert len(trace["tools"]) == 2                # 与实际工具调用数一致
    assert [t["name"] for t in trace["tools"]] == ["remember", "cook"]
    for t in trace["tools"]:
        assert set(t) == {"name", "kind", "ok", "status", "ms"}
        assert t["ok"] is True and t["ms"] >= 0
    assert trace["tools"][0]["kind"] == "memory"
    assert trace["tools"][0]["status"] == "success"    # run_tool 的实际取值，不合并
    assert trace["tools"][1]["kind"] == "action"
    assert trace["tools"][1]["status"] == "done"       # invoke_action 的实际取值
    assert trace["llm_fallback"] is False
    assert trace["summarized"] is False
    assert trace["no_dialogue"] is False
    assert trace["ended_early"] is False              # 有非静默调用（cook），照常继续到收尾轮
    assert trace["first_token_ms"] is not None and trace["first_token_ms"] >= 0
    assert trace["total_ms"] >= trace["first_token_ms"]
    tokens = trace["tokens"]
    assert set(tokens) == {"system", "history", "state", "memory", "user", "tools", "total"}
    assert tokens["total"] == (tokens["system"] + tokens["history"] + tokens["state"]
                               + tokens["memory"] + tokens["user"] + tokens["tools"])
    assert tokens["system"] > 0 and tokens["user"] > 0
    assert tokens["tools"] > 0                       # 工具定义计入估算（D3）
    # 默认只记长度，不记玩家原文与台词原文（隐私 + 日志体积）
    assert trace["message_len"] == len("做碗面")
    assert trace["reply_len"] == len("行啊，面好了，趁热吃。")
    dumped = json.dumps(trace, ensure_ascii=False)
    assert "做碗面" not in dumped and "爱吃面" not in dumped and "趁热吃" not in dumped


def test_turn_trace_tools_tokens_grow_with_actions(tmp_store, write_persona,
                                                   make_app_client, make_provider,
                                                   monkeypatch, tmp_path):
    """D3：工具定义计入估算 —— 动作数量增加时 tools 层与 prompt_tokens 随之增加。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    write_persona("cang")
    client = make_app_client(provider=make_provider(turns=[{"text": "嗯。"}]),
                             executor=FakeExecutor())

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})   # 只有 remember/recall
    client.post("/api/capabilities", json=CAPS)                           # 再声明 3 个动作
    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗",
                                   "mod": "mygame"})

    first, second = read_traces(trace_file)
    assert first["tokens"]["tools"] > 0
    assert second["tokens"]["tools"] > first["tokens"]["tools"]
    assert (second["summary_trigger"]["prompt_tokens"]
            > first["summary_trigger"]["prompt_tokens"])


def test_turn_trace_fallback_turn_has_llm_fallback(tmp_store, write_persona,
                                                   make_app_client, monkeypatch, tmp_path):
    """降级回合（LLM 出错走规则回复）：llm_fallback=true，trace 照出。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    write_persona("cang")
    client = make_app_client(provider=BoomProvider())

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert text_of(res) == "嗯，火塘边坐着说。"          # 角色卡规则回复
    assert kinds(res)[-1] == "done"
    traces = read_traces(trace_file)
    assert len(traces) == 1
    trace = traces[0]
    assert TRACE_KEYS <= set(trace)          # 超集断言：加字段不红，挪字段才红
    assert trace["llm_fallback"] is True
    assert trace["tools"] == []
    assert trace["rounds"] >= 1
    assert trace["no_dialogue"] is False              # 规则回复也算 delta
    assert trace["first_token_ms"] is not None
    assert trace["message_len"] == len("在吗")


def test_turn_trace_write_failure_does_not_break_turn(tmp_store, write_persona,
                                                      make_app_client, make_provider,
                                                      monkeypatch, tmp_path):
    """trace 落盘失败（路径不可写）时回合照常完成，聊天记录照常落盘。"""
    monkeypatch.setenv("NPC_TRACE_FILE", str(tmp_path))   # 目录当文件 → open 必失败
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res)[-1] == "done"
    assert text_of(res) == "嗯。"
    assert [m["content"] for m in chatlog.load_turns("cang", store_dir=tmp_store)] == [
        "在吗", "嗯。"]


def test_turn_trace_no_dialogue_flag(tmp_store, write_persona, make_app_client,
                                     make_provider, monkeypatch, tmp_path):
    """整回合没有 delta：no_dialogue=true、first_token_ms 为 null。

    T4 验收（只有 remember 无台词 → 2 轮且第二轮无工具）：一轮 3 个 remember
    都执行完仍没台词 → 下一轮不给工具（防连环 remember），空文本收尾。
    """
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("NPC_SILENT_REMEMBER", "1")
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [
            {"name": "remember", "arguments": {"content": "事1"}},
            {"name": "remember", "arguments": {"content": "事2"}},
            {"name": "remember", "arguments": {"content": "事3"}}]},
        {"text": ""},                                  # 强制无工具的收尾轮仍不出台词
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "记点事"})

    assert kinds(res)[-1] == "done"
    traces = read_traces(trace_file)
    assert len(traces) == 1
    trace = traces[0]
    assert trace["no_dialogue"] is True
    assert trace["first_token_ms"] is None
    assert trace["rounds"] == 2                       # 1 轮工具 + 1 轮强制收尾
    assert len(trace["tools"]) == 3                    # 3 个 remember 都照常执行
    assert provider.stream_calls[-1]["tools"] is None  # 第二轮无工具
    assert trace["ended_early"] is False               # 没出台词，静默收尾不触发
    assert trace["llm_fallback"] is False
    assert trace["reply_len"] == 0


def test_turn_trace_summarized_flag(tmp_store, write_persona, make_app_client,
                                    make_provider, monkeypatch, tmp_path):
    """触发摘要的回合：summarized=true（本回合启动了后台摘要）。

    摘要已移出关键路径：回合的 stream 先跑（台词「嗯。」），摘要的 llm.chat
    在回合结束后台才跑（回「摘要内容」）—— 假 Provider 的 turns 顺序按此排列。
    """
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("NPC_CONTEXT_WINDOW", "8000")
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "0.01")        # 阈值 = 80：提示必越线
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    monkeypatch.setenv("NPC_BUDGET_HISTORY", "100000")
    write_persona("cang")
    for i in range(5):
        chatlog.append_turn("cang", "问" * 20 + str(i), "答" * 20 + str(i),
                            store_dir=tmp_store)
    provider = make_provider(turns=[{"text": "嗯。"}, {"text": "摘要内容"}])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert text_of(res) == "嗯。"
    traces = read_traces(trace_file)
    assert len(traces) == 1
    assert traces[0]["summarized"] is True
    assert traces[0]["llm_fallback"] is False


def test_turn_trace_summary_trigger_field(tmp_store, write_persona, make_app_client,
                                          make_provider, monkeypatch, tmp_path):
    """trace 含 summary_trigger：prompt_tokens = 本回合 tokens.total，limit = 窗口 × 比例。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("NPC_CONTEXT_WINDOW", "8000")
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "0.05")        # limit = 400
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res)[-1] == "done"
    traces = read_traces(trace_file)
    assert len(traces) == 1
    trigger = traces[0]["summary_trigger"]
    assert trigger["limit"] == 400
    assert trigger["prompt_tokens"] == traces[0]["tokens"]["total"]
    assert traces[0]["summarized"] is False               # 只 1 轮，没有可压的旧轮次


def test_turn_trace_fallback_does_not_trigger_summary(tmp_store, write_persona,
                                                      make_app_client, monkeypatch,
                                                      tmp_path):
    """降级回合不触发摘要：即使提示 token 越线、旧轮次够压，也等下回合再说。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("NPC_CONTEXT_WINDOW", "8000")
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "0.01")        # limit = 80：提示必越线
    monkeypatch.setenv("NPC_BUDGET_HISTORY", "100000")
    write_persona("cang")
    for i in range(6):
        chatlog.append_turn("cang", "问" * 20 + str(i), "答" * 20 + str(i),
                            store_dir=tmp_store)
    client = make_app_client(provider=BoomProvider())

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res)[-1] == "done"
    traces = read_traces(trace_file)
    assert len(traces) == 1
    trace = traces[0]
    assert trace["llm_fallback"] is True
    assert trace["summarized"] is False
    assert trace["summary_trigger"]["limit"] == 80
    assert trace["summary_trigger"]["prompt_tokens"] >= 80  # 有估算也不触发（降级回合）
    assert not chatlog.summary_path("cang", store_dir=tmp_store).exists()
