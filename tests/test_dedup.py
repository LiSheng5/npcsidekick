"""重复动作保护 —— 在途（进行中 / 待确认）的同名同参动作不重复下发。

`call_id` 幂等只保护「同一个 `call_id` 被重发」；LLM 自己再调一次同名动作会生成
**新的** `call_id`，Mod 无从识别。默认策略：动作名 + 参数都相同才拦截
（参数按规范化 JSON 比较），参数不同 = 新指令（如"再砍 3 根"），正常放行。

被拦截的调用：不发给 mod、不写记忆、不进 action 帧（executed），trace 的 tools 里
`status` 记 `deduped`。
"""
from __future__ import annotations

import memory
from conftest import CAPS, FakeExecutor, frames, kinds


def test_timeout_then_same_action_is_not_resent(tmp_store, write_persona, make_app_client,
                                                make_provider, monkeypatch):
    """动作超时后 LLM 再调同名同参动作：FakeExecutor 只被调一次，LLM 收到提示。"""
    write_persona("cang")
    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "0.05")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "稍等。"},
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "面还在锅里，别催。"},
    ])
    executor = FakeExecutor(delay=0.5)                 # 第一次调用必然超时 → 登记 unknown
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})
    assert len(executor.calls) == 1 and len(client.app.state.pending) == 1

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "再做一次", "mod": "mygame"})

    assert len(executor.calls) == 1                    # 重复调用没有发给 mod
    tool_msg = provider.stream_calls[-1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "没有重复下达" in tool_msg["content"]        # LLM 收到"这次没重复下达"的提示
    assert kinds(res).count("action") == 0             # 不计入 executed（无 action 帧）
    assert memory.load_card("cang", store_dir=tmp_store) == []      # 不写记忆
    assert len(client.app.state.pending) == 1          # 原在途条目原样保留


def test_same_name_different_params_goes_through(tmp_store, write_persona, make_app_client,
                                                 make_provider):
    """参数不同的同名动作是新指令，正常放行（默认策略：名 + 参都相同才拦截）。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "汤"}}]},
        {"text": "汤也煮上。"},
    ])
    executor = FakeExecutor()
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)
    client.app.state.pending.register("c1", "cang", "cook", {"dish": "面"}, "mygame",
                                      state="unknown")   # 只有「煮面」在途

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "再煮个汤", "mod": "mygame"})

    assert len(executor.calls) == 1                    # 煮汤正常下发给 mod
    record = [f["action"] for f in frames(res) if f["type"] == "action"][0]
    assert record["params"] == {"dish": "汤"} and record["status"] == "done"
    assert [e["content"] for e in memory.load_card("cang",
                                                   store_dir=tmp_store)] == ["完成: 面煮好了"]
    tool_msg = provider.stream_calls[-1]["messages"][-1]
    assert "没有重复下达" not in tool_msg["content"]


def test_running_action_also_deduped_with_trace_status(tmp_store, write_persona,
                                                       make_app_client, make_provider,
                                                       monkeypatch, tmp_path):
    """running 的在途动作同样被拦；trace 的 tools 里 status 如实记 `deduped`。"""
    import json

    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "火还没关。"},
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "说了在煮。"},
    ])
    executor = FakeExecutor(result={"ok": False, "status": "running", "note": "还在煮"})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})
    assert len(client.app.state.pending) == 1          # running 也登记在途

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "好了没", "mod": "mygame"})

    assert len(executor.calls) == 1                    # running 的同名同参照样拦
    assert kinds(res).count("action") == 0
    traces = [json.loads(line) for line in trace_file.read_text("utf-8").splitlines()
              if line.strip()]
    assert len(traces) == 2
    tool = traces[-1]["tools"][0]                      # 被拦的那次调用
    assert tool["name"] == "cook" and tool["kind"] == "action"
    assert tool["status"] == "deduped" and tool["ok"] is False
