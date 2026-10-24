"""server.py 测试 —— /api/talk 的动作执行链：大脑同步转发 mod 执行、工具轮预算、
注册表（心跳/白名单/执行接口）语义、执行器与工具调用缓冲。"""

from __future__ import annotations

import json

import memory
import tools
from conftest import CAPS, FakeExecutor, frames, kinds, text_of

# ── /api/talk：动作 = LLM 直接调用的工具（大脑同步转发 mod 执行）──

def test_talk_action_executes_via_mod_then_writes_memory(tmp_store, write_persona,
                                                         make_app_client, make_provider):
    """大脑同步转发 mod 的 execute_url → 结果当场回上下文继续推理 + 写进记忆卡（协议 §3）。

    流末尾的 action 帧只是"本回合已执行的动作"的记录，mod 不需要照它做任何事。
    """
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "行啊，", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "面好了，趁热吃。"},
    ])
    executor = FakeExecutor()
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你能帮我做顿饭吗",
                                         "mod": "mygame"})

    assert kinds(res)[-1] == "done"
    assert kinds(res).count("action") == 1
    record = frames(res)[-2]["action"]
    assert record["name"] == "cook" and record["params"] == {"dish": "面"}
    assert record["ok"] is True and record["status"] == "done"   # 结果也进帧
    assert text_of(res) == "行啊，面好了，趁热吃。"
    # mod 真的被同步调用了，载荷符合协议 §3（call_id = 大脑生成，Mod 回报时原样带回）
    call = executor.calls[0]
    assert call["url"] == CAPS["execute_url"] and call["timeout"] == 30.0
    payload = call["payload"]
    assert len(payload["call_id"]) == 32                        # uuid4().hex
    assert payload["call_id"] == record["call_id"]              # 帧里的 id 与发给 mod 的一致
    assert {k: v for k, v in payload.items() if k != "call_id"} == {
        "npc_id": "cang", "action": "cook", "params": {"dish": "面"}, "mod": "mygame"}
    # 结果当场写卡 + 第二轮上下文里带上工具结果
    assert [e["content"] for e in memory.load_card("cang",
                                                   store_dir=tmp_store)] == ["完成: 面煮好了"]
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "面煮好了" in tool_msg["content"]

def test_talk_action_failure_is_not_pretended(tmp_store, write_persona,
                                              make_app_client, make_provider):
    """执行接口连不上 = 失败：不写"完成"，让 LLM 自己圆场（协议 §3）。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "灶好像坏了，没做成。"},
    ])
    executor = FakeExecutor(error=RuntimeError("connect refused"))
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "没做成: 执行接口没接上（RuntimeError）"]
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith("没做成")
    assert text_of(res) == "灶好像坏了，没做成。"
    # 失败的动作在帧里必须看得出来：旧字段仍在，新增 ok / status
    record = [f["action"] for f in frames(res) if f["type"] == "action"][0]
    assert record["name"] == "cook" and record["params"] == {"dish": "面"}
    assert record["ok"] is False and record["status"] == "failed"
    assert len(record["call_id"]) == 32

def test_talk_action_timeout_is_unknown_not_failure(tmp_store, write_persona, make_app_client,
                                                    make_provider, monkeypatch):
    """等待超过 NPC_EXECUTE_TIMEOUT = **不知道做没做**：不写失败卡，登记 pending 等回报。

    （超时 ≠ 没做成 —— mod 可能已经执行了，只是没等到回应。）
    """
    write_persona("cang")
    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "0.05")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "灶是不是坏了……" },
    ])
    executor = FakeExecutor(delay=0.5)
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    assert executor.calls[0]["timeout"] == 0.05
    assert memory.load_card("cang", store_dir=tmp_store) == []       # 不当成"没做成"
    assert len(client.app.state.pending) == 1                        # 登记待定，等 mod 回报
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert "不确定" in tool_msg["content"]                            # 给模型留圆场余地
    assert text_of(res) == "灶是不是坏了……"
    assert kinds(res).count("action") == 1

def test_talk_action_timeout_late_report_writes_memory(tmp_store, write_persona, make_app_client,
                                                       make_provider, monkeypatch):
    """超时后 mod 晚到的成功回报（带 call_id）→ 正确写"完成"卡。"""
    write_persona("cang")
    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "0.05")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "稍等。"},
    ])
    executor = FakeExecutor(delay=0.5)
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})
    call_id = executor.calls[0]["payload"]["call_id"]

    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "其实煮好了",
        "call_id": call_id, "mod": "mygame"})

    assert res.json()["matched"] is True
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: 其实煮好了"]
    assert len(client.app.state.pending) == 0

def test_talk_action_timeout_without_report_expires_into_failure(tmp_store, write_persona,
                                                                 make_app_client, make_provider,
                                                                 monkeypatch):
    """超时登记的动作一直没回报 → TTL 到点写一条失败记忆（"没做成: 一直没等到结果"）。"""
    write_persona("cang")
    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "0.05")
    monkeypatch.setenv("NPC_PENDING_TTL", "0")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "稍等。"},
        {"text": "嗯。"},
    ])
    executor = FakeExecutor(delay=0.5)
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})
    assert len(client.app.state.pending) == 1
    assert memory.load_card("cang", store_dir=tmp_store) == []

    client.post("/api/talk", json={"npc_id": "cang", "message": "好了没"})   # 下一轮开头顺手 expire

    assert len(client.app.state.pending) == 0
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "没做成: 一直没等到结果"]

def test_talk_action_running_is_not_recorded(tmp_store, write_persona, make_app_client,
                                             make_provider):
    """mod 回 status=running：结果还没定 —— 不写卡（等 §4 回报），只告诉 LLM 进行中。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "火还没关，再等会儿。"},
    ])
    executor = FakeExecutor(result={"ok": False, "status": "running", "note": "cook 超时没做完"})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    assert memory.load_card("cang", store_dir=tmp_store) == []  # 没定论就不写卡
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert "还在进行中" in tool_msg["content"]
    assert kinds(res).count("action") == 1                     # 调过就有记录
    record = frames(res)[-2]["action"]
    assert record["ok"] is False and record["status"] == "running"   # 帧里如实带结果

def test_talk_running_action_registers_pending_then_report_writes_once(tmp_store, write_persona,
                                                                      make_app_client,
                                                                      make_provider):
    """mod 回 running → 登记 call_id；带 call_id 回报写卡**一次**；重复回报不再写（协议 §4）。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "火还没关，再等会儿。"},
    ])
    executor = FakeExecutor(result={"ok": False, "status": "running", "note": "还在煮"})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    pending = client.app.state.pending
    call_id = executor.calls[0]["payload"]["call_id"]
    assert len(pending) == 1                                   # running → 登记待定
    assert memory.load_card("cang", store_dir=tmp_store) == []  # 还没定论就不写卡

    first = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面煮好了",
        "call_id": call_id, "mod": "mygame"})

    assert first.status_code == 200 and first.json()["matched"] is True
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: 面煮好了"]
    assert len(pending) == 0                                   # 精确销账

    again = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面煮好了", "call_id": call_id})

    assert again.status_code == 200 and again.json()["matched"] is False
    assert len(memory.load_card("cang", store_dir=tmp_store)) == 1   # 重复回报不写第二遍

def test_talk_action_unknown_status_is_not_recorded_as_done(tmp_store, write_persona,
                                                            make_app_client, make_provider):
    """mod 回未知 status（协议误用）→ 告警按"没做成"落点，绝不静默当 done 记完成。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "糊了。"},
    ])
    executor = FakeExecutor(result={"ok": True, "status": "weird", "note": "回了个怪词"})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    record = frames(res)[-2]["action"]
    assert record["status"] == "failed" and record["ok"] is False
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "没做成: 回了个怪词"]

def test_talk_action_failed_status_is_not_recorded_as_done(tmp_store, write_persona,
                                                           make_app_client, make_provider):
    """mod 回 status=failed 就是失败 —— ok 压成假，不许记"完成"。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "糊了。"},
    ])
    executor = FakeExecutor(result={"ok": True, "status": "failed", "note": "锅烧干了"})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    record = frames(res)[-2]["action"]
    assert record["status"] == "failed" and record["ok"] is False
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "没做成: 锅烧干了"]

def test_talk_chat_action_roundtrip_with_report(tmp_store, write_persona,
                                                make_app_client, make_provider):
    """chat 动作（CAPS 第三个动作）全链路：下发执行 → running 登记 → action 帧 →
    带 call_id 回报写卡销账。此前 chat 只有"工具定义包含"断言，执行路径零覆盖。
    """
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "chat",
                                     "arguments": {"target": "贝拉", "topic": "晚饭"}}]},
        {"text": "她说了想喝汤。"},
    ])
    executor = FakeExecutor(result={"ok": True, "status": "running", "note": "贝拉说想喝汤"})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "去问问贝拉想吃什么",
                                         "mod": "mygame"})

    # 真的下发给 mod，载荷带 chat 的参数（协议 §3）
    call = executor.calls[0]
    assert call["payload"]["action"] == "chat"
    assert call["payload"]["params"] == {"target": "贝拉", "topic": "晚饭"}
    # running → 不写卡，登记 pending 等 §4 回报
    assert memory.load_card("cang", store_dir=tmp_store) == []
    assert len(client.app.state.pending) == 1
    # action 帧如实记录 running（mod 不需要照它做任何事，仅记录）
    record = [f["action"] for f in frames(res) if f["type"] == "action"][0]
    assert record["name"] == "chat" and record["status"] == "running"
    assert record["ok"] is True
    assert text_of(res) == "她说了想喝汤。"

    # 带 call_id 回报 → 写卡一次并精确销账
    report = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "chat", "ok": True, "note": "聊过了，她说想喝汤",
        "call_id": call["payload"]["call_id"], "mod": "mygame"})

    assert report.json()["matched"] is True
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: 聊过了，她说想喝汤"]
    assert len(client.app.state.pending) == 0

def test_talk_two_action_rounds_emit_two_frames(tmp_store, write_persona, make_app_client,
                                                make_provider):
    """同一回合连续调两个动作工具：两轮转发、两条写卡、两个 action 帧。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "", "tool_calls": [{"name": "goto", "arguments": {"place": "厨房"}}]},
        {"text": "做好了，我端过去。"},
    ])
    executor = FakeExecutor(results=[
        {"ok": True, "status": "done", "note": "面煮好了"},
        {"ok": True, "status": "done", "note": "走到厨房了"},
    ])
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭端过来",
                                         "mod": "mygame"})

    assert len(executor.calls) == 2
    assert kinds(res).count("action") == 2
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: 面煮好了", "完成: 走到厨房了"]

def test_talk_tool_budget_exhausted_still_speaks(tmp_store, write_persona, make_app_client,
                                                 make_provider):
    """工具轮预算用满后仍有一次收尾轮：动作做了，台词不会缺席（不带工具，逼模型说话）。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "", "tool_calls": [{"name": "goto", "arguments": {"place": "厨房"}}]},
        {"text": "", "tool_calls": [{"name": "remember",
                                     "arguments": {"content": "玩家要一碗面", "importance": 5}}]},
        {"text": "都办好了，趁热吃。"},
    ])
    executor = FakeExecutor(results=[{"ok": True, "status": "done", "note": "面煮好了"},
                                     {"ok": True, "status": "done", "note": "走到厨房了"}])
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭端过来",
                                         "mod": "mygame"})

    assert len(executor.calls) == 2                            # 两个动作都执行了
    assert text_of(res) == "都办好了，趁热吃。"                  # 收尾轮一定有台词
    assert provider.stream_calls[-1]["tools"] is None          # 收尾轮不给工具
    assert kinds(res)[-1] == "done"

def test_talk_closing_round_without_tools_forces_text(tmp_store, write_persona, make_app_client,
                                                      make_provider):
    """模型每轮都想调工具也拦不住最后一轮说话：收尾轮不给工具定义，只出文本。

    回归：工具预算用满后如果直接结束循环，动作做了、台词一句没有（NPC 哑巴收场）。
    """
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "", "tool_calls": [{"name": "goto", "arguments": {"place": "厨房"}}]},
        {"text": "", "tool_calls": [{"name": "remember",
                                     "arguments": {"content": "玩家要一碗面", "importance": 5}}]},
        # 收尾轮：模型仍然"想"调工具（假 provider 不看 tools，照吐调用）——必须被忽略
        {"text": "都办好了，趁热吃。", "tool_calls": [{"name": "cook", "arguments": {"dish": "汤"}}]},
    ])
    executor = FakeExecutor(results=[{"ok": True, "status": "done", "note": "面煮好了"},
                                     {"ok": True, "status": "done", "note": "走到厨房了"}])
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭端过来",
                                         "mod": "mygame"})

    assert len(executor.calls) == 2                            # 收尾轮的工具调用不再执行
    assert text_of(res) == "都办好了，趁热吃。"                  # 台词一定有（delta 帧）
    assert "delta" in kinds(res)
    last = provider.stream_calls[-1]
    assert last["tools"] is None                               # 收尾轮不给工具定义
    assert all(c["tools"] for c in provider.stream_calls[:-1])  # 前面的轮次照常给
    assert kinds(res)[-1] == "done"

def test_talk_bad_tool_arguments_goes_back_to_model(tmp_store, write_persona, make_app_client,
                                                    make_provider):
    """工具参数不是合法 JSON：不拿空参数去执行动作，而是回给模型让它重发。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "raw_arguments": "{坏"}]},
        {"text": "我再看看怎么做。"},
    ])
    executor = FakeExecutor()
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    assert executor.calls == []                                # 没执行任何动作
    assert memory.load_card("cang", store_dir=tmp_store) == []  # 也没写卡
    assert kinds(res).count("action") == 0
    assert text_of(res) == "我再看看怎么做。"
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "合法 JSON" in tool_msg["content"]

def test_talk_mixed_memory_and_action_in_one_round(tmp_store, write_persona, make_app_client,
                                                   make_provider):
    """同一条 assistant 消息里混合记忆工具与动作工具：都执行，顺序按调用顺序。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [
            {"name": "remember", "arguments": {"content": "玩家爱吃面", "importance": 7}},
            {"name": "cook", "arguments": {"dish": "面"}},
        ]},
        {"text": "记住了，也做好了。"},
    ])
    executor = FakeExecutor()
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "记住我爱吃面，做一碗",
                                   "mod": "mygame"})

    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "玩家爱吃面", "完成: 面煮好了"]

def test_talk_without_execute_url_gets_no_action_tools(tmp_store, write_persona,
                                                       make_app_client, make_provider):
    """没给执行接口 = 没有可执行的动作：动作不进工具定义（协议 §1）。"""
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    executor = FakeExecutor()
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json={"mod": "mygame", "actions": CAPS["actions"]})

    client.post("/api/talk", json={"npc_id": "cang", "message": "做顿饭", "mod": "mygame"})

    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert names == ["remember", "recall"]
    assert executor.calls == []

def test_talk_tool_definitions_follow_whitelist(tmp_store, write_persona, make_app_client,
                                                make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗", "mod": "mygame"})

    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert names == ["remember", "recall", "cook", "goto", "chat"]

def test_talk_offline_mod_gets_no_action_tools(tmp_store, write_persona, make_app_client,
                                               make_provider):
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "嗯，我记下了。"},
    ])
    client = make_app_client(provider=provider, heartbeat_timeout_override=0.0)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert names == ["remember", "recall"]                         # mod 离线 → 不给动作工具
    assert "action" not in kinds(res)

def test_talk_heartbeat_kept_alive_by_request(tmp_store, write_persona, make_app_client,
                                              make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗", "mod": "mygame"})

    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert "cook" in names                                         # 请求里捎带 mod 即刷新心跳，视为活着


# ── 报到校验 / 注册表语义 / 执行超时 ─────────────────────

def test_capabilities_execute_url_type_and_state(tmp_store, write_persona, make_app_client):
    """execute_url 必须是字符串（非法 → 400）；报到后能在 /api/state 看到。"""
    client = make_app_client()
    assert client.post("/api/capabilities",
                       json=dict(CAPS, execute_url=123)).status_code == 400

    assert client.post("/api/capabilities", json=CAPS).status_code == 200
    assert client.get("/api/state").json()["mods"]["mygame"]["execute_url"] == CAPS["execute_url"]

def test_execute_timeout_env(monkeypatch):
    """NPC_EXECUTE_TIMEOUT 现读、可热切；非法值回默认 30s。"""
    import server

    monkeypatch.delenv("NPC_EXECUTE_TIMEOUT", raising=False)
    assert server.execute_timeout() == 30.0

    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "5")
    assert server.execute_timeout() == 5.0

    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", " 坏 ")
    assert server.execute_timeout() == 30.0

def test_registry_execute_url_semantics():
    """执行接口地址：离线 / 没声明 → None；只有恰好一个在线 mod 时才兜底。"""
    import server

    reg = server.Registry(timeout=60.0)
    reg.declare("mygame", [{"name": "cook", "desc": "做饭", "params": {}}],
                execute_url="http://127.0.0.1:8766/execute")
    assert reg.execute_url("mygame") == "http://127.0.0.1:8766/execute"
    assert reg.execute_url(None) == "http://127.0.0.1:8766/execute"   # 只有一个在线 mod → 用它

    reg.declare("other", [{"name": "x", "desc": "y", "params": {}}])  # 没给执行接口
    assert reg.execute_url(None) is None                              # 两个在线 → 不猜
    assert reg.execute_url("other") is None

    offline = server.Registry(timeout=0.0)
    offline.declare("mygame", [{"name": "cook", "desc": "做饭", "params": {}}],
                    execute_url="http://127.0.0.1:8766/execute")
    assert offline.execute_url("mygame") is None                       # 离线 → 不调动作

def test_registry_actions_empty_when_offline():
    import server

    reg = server.Registry(timeout=0.0)
    reg.declare("mygame", [{"name": "cook", "desc": "做饭", "params": {}}])
    assert reg.actions("mygame") == []
    assert reg.actions(None) == []

    reg2 = server.Registry(timeout=60.0)
    reg2.declare("mygame", [{"name": "cook", "desc": "做饭", "params": {}}])
    assert reg2.actions("mygame")[0]["name"] == "cook"
    assert reg2.actions(None)[0]["name"] == "cook"          # 只有一个在线 mod → 用它


# ── 执行器与工具调用缓冲（单元）─────────────────────────

def test_http_execute_real_roundtrip():
    """默认执行器走一条真的本地 HTTP（零外网）：POST JSON → 解析 JSON 响应。"""
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import turn

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen["payload"] = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps({"ok": True, "status": "done", "note": "面煮好了"},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        url = "http://127.0.0.1:%d/execute" % httpd.server_address[1]
        result = asyncio.run(turn._http_execute(url, {
            "npc_id": "cang", "action": "cook", "params": {"dish": "面"}, "mod": "mygame"}, 5.0))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert result == {"ok": True, "status": "done", "note": "面煮好了"}
    assert seen["payload"]["action"] == "cook"

def test_tool_call_buffer_reassembles_split_chunks():
    import turn

    buf = turn._ToolCallBuffer()
    buf.feed({"index": 0, "id": "call_1", "function_name": "co", "function_arguments": None})
    buf.feed({"index": 0, "id": None, "function_name": "ok", "function_arguments": '{"dish"'})
    buf.feed({"index": 0, "id": None, "function_name": None, "function_arguments": ': "面"}'})

    call = buf.finalize(0)
    assert call == {"id": "call_1", "name": "cook", "args": {"dish": "面"}}

def test_tool_call_buffer_tolerates_bad_json():
    import turn

    buf = turn._ToolCallBuffer()
    buf.feed({"index": 0, "function_name": "cook", "function_arguments": "{坏"})
    call = buf.finalize(0)
    assert call["args"] == {}
    assert call["bad_args"] is True                            # 标记出来，执行前要回给模型重发
    assert call["name"] == "cook" and call["id"]               # 不抛异常，照样给出调用形态

    # 合法 JSON 但不是对象（数组 / 字符串）同样按坏参数处理
    buf2 = turn._ToolCallBuffer()
    buf2.feed({"index": 0, "function_name": "cook", "function_arguments": "[1, 2]"})
    assert buf2.finalize(0)["bad_args"] is True

def test_tools_module_whitelist_matches_server():
    """server 用 tools 的白名单判定动作，不另立一套。"""
    assert tools.is_action_tool("cook", CAPS["actions"]) is True
    assert tools.is_action_tool("dance", CAPS["actions"]) is False
