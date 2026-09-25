"""server.py 测试 —— 协议.md 四端点的契约行为（全零网络）。"""
from __future__ import annotations

import json

import pytest

import memory
import tools


CAPS = {
    "mod": "sims4",
    "actions": [
        {"name": "cook", "desc": "用厨房做饭", "params": {"dish": "菜名(字符串)"}},
        {"name": "goto", "desc": "走到某地", "params": {"place": "地点名(字符串)"}},
        {"name": "chat", "desc": "主动找某人说话",
         "params": {"target": "对象名", "topic": "话题"}},
    ],
}


def frames(response) -> list[dict]:
    """把 SSE 响应体拆成帧列表。"""
    out = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: "):]))
    return out


def kinds(response) -> list[str]:
    return [f["type"] for f in frames(response)]


def text_of(response) -> str:
    return "".join(f.get("text", "") for f in frames(response) if f["type"] == "delta")


# ── /api/capabilities（协议.md §1）───────────────────────

def test_capabilities_ok(tmp_store, write_persona, make_app_client):
    client = make_app_client()

    res = client.post("/api/capabilities", json=CAPS)

    assert res.status_code == 200
    assert res.json() == {"ok": True, "mod": "sims4", "actions": ["cook", "goto", "chat"]}
    assert client.get("/api/state").json()["mods"]["sims4"]["actions"] == 3


@pytest.mark.parametrize("bad", [
    {},
    {"mod": "sims4"},
    {"mod": "", "actions": CAPS["actions"]},
    {"mod": "sims4", "actions": []},
    {"mod": "sims4", "actions": "不是数组"},
    {"mod": "sims4", "actions": [{"desc": "缺名字"}]},
    {"mod": "sims4", "actions": [{"name": "cook"}]},
    {"mod": "sims4", "actions": [{"name": "cook", "desc": "x", "params": "不是对象"}]},
])
def test_capabilities_invalid_400(tmp_store, write_persona, make_app_client, bad):
    client = make_app_client()
    assert client.post("/api/capabilities", json=bad).status_code == 400


def test_malformed_json_body_400(tmp_store, write_persona, make_app_client):
    client = make_app_client()
    res = client.post("/api/capabilities", content=b"{bad json",
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 400


# ── /api/talk：纯聊天（协议.md §2）───────────────────────

def test_talk_plain_text(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "行啊，我去给你煮碗面。"}])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你能帮我做顿饭吗"})

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert kinds(res)[-1] == "done" and set(kinds(res)) == {"delta", "done"}
    assert text_of(res) == "行啊，我去给你煮碗面。"
    # 台词进了持久聊天记录（§4.1）
    assert [m["content"] for m in __import__("chatlog").load_turns("cang")] == [
        "你能帮我做顿饭吗", "行啊，我去给你煮碗面。"]


def test_talk_observation_reaches_prompt(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    client.post("/api/talk", json={
        "npc_id": "cang", "message": "在吗",
        "observation": {"summary": "玩家在厨房，刚下班", "nearby": ["玩家", "冰箱"]}})

    system = provider.stream_calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "玩家在厨房，刚下班" in system["content"]
    assert "台词" in system["content"]                     # §2.2 台词纪律进了提示词


def test_talk_missing_fields_400(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    assert client.post("/api/talk", json={"message": "在吗"}).status_code == 400
    assert client.post("/api/talk", json={"npc_id": "cang"}).status_code == 400
    assert client.post("/api/talk", json={"npc_id": "cang", "message": "  "}).status_code == 400


def test_talk_unknown_npc_400(tmp_store, write_persona, make_app_client):
    client = make_app_client()
    res = client.post("/api/talk", json={"npc_id": "nobody", "message": "在吗"})
    assert res.status_code == 400
    assert "nobody" in res.json()["detail"]


def test_talk_broken_persona_400(tmp_store, persona_dir, make_app_client):
    (persona_dir / "cang.json").write_text("{坏", encoding="utf-8")
    client = make_app_client()
    assert client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"}).status_code == 400


# ── /api/talk：记忆工具循环 ──────────────────────────────

def test_talk_runs_remember_then_replies(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "remember",
                                     "arguments": {"content": "玩家爱吃面", "importance": 7}}]},
        {"text": "记住了，你爱吃面。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "记住：我爱吃面"})

    assert kinds(res)[-1] == "done" and set(kinds(res)) == {"delta", "done"}
    assert text_of(res) == "记住了，你爱吃面。"
    entries = memory.load_card("cang")
    assert [e["content"] for e in entries] == ["玩家爱吃面"]      # remember 真的落卡
    assert len(provider.stream_calls) == 2
    # 第二轮上下文里带上了工具结果
    roles = [m["role"] for m in provider.stream_calls[1]["messages"]]
    assert roles[-1] == "tool"


def test_talk_recall_loop(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    memory.add_entry("cang", "玩家上次说爱吃面", importance=8)
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "recall", "arguments": {"query": "面"}}]},
        {"text": "你上次说过爱吃面。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "我上次说爱吃啥"})

    assert text_of(res) == "你上次说过爱吃面。"
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert "玩家上次说爱吃面" in tool_msg["content"]              # 逐字记忆进了上下文


# ── /api/talk：动作提议（协议.md §3）──────────────────────

def test_talk_action_proposal(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "行啊，", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
    ])
    client = make_app_client(provider=provider)
    client.post("/api/capabilities", json=CAPS)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你能帮我做顿饭吗",
                                         "mod": "sims4"})

    assert kinds(res) == ["delta", "delta", "action", "done"]     # 动作帧在流末尾（§5）
    action_frame = frames(res)[-2]
    assert action_frame["action"] == {"name": "cook", "params": {"dish": "面"}}
    assert text_of(res) == "行啊，"
    assert memory.load_card("cang") == []                          # 只提议，不执行（§3）


def test_talk_tool_definitions_follow_whitelist(tmp_store, write_persona, make_app_client,
                                                make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗", "mod": "sims4"})

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

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "sims4"})

    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert names == ["remember", "recall"]                         # §6：离线不提议动作
    assert "action" not in kinds(res)


def test_talk_heartbeat_kept_alive_by_request(tmp_store, write_persona, make_app_client,
                                              make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗", "mod": "sims4"})

    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert "cook" in names                                         # 捎带 mod 即视为活着（§1）


# ── /api/talk：LLM 不可用降级（协议.md §6）───────────────

def test_talk_falls_back_to_persona_rules(tmp_store, write_persona, make_app_client):
    from conftest import BoomProvider

    write_persona("cang")
    client = make_app_client(provider=BoomProvider())

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "今天说说狩猎的事"})

    assert res.status_code == 200
    assert kinds(res) == ["delta", "done"]
    assert text_of(res) == "别追跑得快的。"          # 命中角色卡 rules.replies 的关键词
    assert "action" not in kinds(res)


def test_talk_fallback_uses_default_when_no_keyword(tmp_store, write_persona, make_app_client):
    from conftest import BoomProvider

    write_persona("cang")
    client = make_app_client(provider=BoomProvider())

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "随便聊聊"})

    assert text_of(res) == "嗯，火塘边坐着说。"


# ── /api/action_result（协议.md §4）──────────────────────

def test_action_result_writes_memory(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面煮好了，玩家吃了说不错"})

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert memory.load_card("cang")[0]["content"] == "完成: 面煮好了，玩家吃了说不错"


def test_action_result_failure_wording(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    client.post("/api/action_result", json={"npc_id": "cang", "action": "goto", "ok": False})

    assert memory.load_card("cang")[0]["content"] == "没做成: goto"


@pytest.mark.parametrize("bad", [
    {},
    {"npc_id": "cang"},
    {"npc_id": "cang", "action": "cook"},
    {"npc_id": "cang", "action": "cook", "ok": "true"},
    {"npc_id": "cang", "action": "cook", "ok": True, "note": 5},
])
def test_action_result_invalid_400(tmp_store, write_persona, make_app_client, bad):
    write_persona("cang")
    client = make_app_client()
    assert client.post("/api/action_result", json=bad).status_code == 400


# ── 状态查询（协议.md §5）───────────────────────────────

def test_state_endpoint(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()
    client.post("/api/capabilities", json=CAPS)

    state = client.get("/api/state").json()

    assert state["model"] == "fake-model"
    assert state["store_dir"] == str(memory.STORE_DIR)
    assert state["npcs"] == 1
    assert state["mods"]["sims4"]["online"] is True
    assert state["heartbeat_timeout_s"] == 60.0


def test_npcs_endpoint(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    write_persona("ali", name="阿黎")
    memory.add_entry("cang", "一条记忆")
    client = make_app_client(provider=make_provider(turns=[{"text": "嗯。"}]))
    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    data = client.get("/api/npcs").json()["npcs"]

    by_id = {n["npc_id"]: n for n in data}
    assert set(by_id) == {"cang", "ali"}
    assert by_id["cang"]["memory_entries"] == 1
    assert by_id["cang"]["chat_messages"] == 2
    assert by_id["cang"]["last_activity"] > 0
    assert by_id["ali"]["chat_messages"] == 0


# ── 工具与提示词单元行为 ─────────────────────────────────

def test_build_system_prompt_includes_persona_fields():
    import server

    prompt = server.build_system_prompt({
        "identity": "部落的老猎手", "personality": "寡言直接", "speech_style": "短句",
        "voice_samples": ["别追跑得快的。"], "taboos": ["烧湿柴"],
    }, {"summary": "玩家在厨房"})

    assert "部落的老猎手" in prompt and "寡言直接" in prompt and "短句" in prompt
    assert "别追跑得快的。" in prompt
    assert "你绝不会烧湿柴" in prompt
    assert "玩家在厨房" in prompt
    assert "recall" in prompt                     # §4.4：回忆类问题优先走 recall 的逐字结果
    assert "不知道" in prompt                     # §4.4：接地，不知道就说不知道


def test_reasoning_effort_env(monkeypatch):
    import server

    monkeypatch.delenv("NPC_REASONING_EFFORT", raising=False)
    assert server.reasoning_effort() is None

    monkeypatch.setenv("NPC_REASONING_EFFORT", "  ")
    assert server.reasoning_effort() is None

    monkeypatch.setenv("NPC_REASONING_EFFORT", "off")
    assert server.reasoning_effort() == "off"

    monkeypatch.setenv("NPC_REASONING_EFFORT", " high ")
    assert server.reasoning_effort() == "high"


def test_reasoning_effort_reaches_provider(tmp_store, write_persona, make_app_client,
                                          make_provider, monkeypatch):
    import server

    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}, {"text": "嗯。"}])
    client = make_app_client(provider=provider)

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})
    assert provider.stream_calls[0]["reasoning_effort"] is None    # 未设置 → 服务端默认

    monkeypatch.setenv("NPC_REASONING_EFFORT", "off")
    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})
    assert provider.stream_calls[1]["reasoning_effort"] == "off"   # 现读环境变量，可热切
    assert client.get("/api/state").json()["reasoning_effort"] == "off"


def test_rule_reply_keyword_and_fallback():
    import server

    persona = {"rules": {"replies": {"柴": "挑干透的。"}, "fallback": "嗯。"}}
    assert server.rule_reply(persona, "柴火怎么选") == "挑干透的。"
    assert server.rule_reply(persona, "你好") == "嗯。"
    assert server.rule_reply({}, "你好") == "……"


def test_tool_call_buffer_reassembles_split_chunks():
    import server

    buf = server._ToolCallBuffer()
    buf.feed({"index": 0, "id": "call_1", "function_name": "co", "function_arguments": None})
    buf.feed({"index": 0, "id": None, "function_name": "ok", "function_arguments": '{"dish"'})
    buf.feed({"index": 0, "id": None, "function_name": None, "function_arguments": ': "面"}'})

    call = buf.finalize(0)
    assert call == {"id": "call_1", "name": "cook", "args": {"dish": "面"}}


def test_tool_call_buffer_tolerates_bad_json():
    import server

    buf = server._ToolCallBuffer()
    buf.feed({"index": 0, "function_name": "cook", "function_arguments": "{坏"})
    assert buf.finalize(0)["args"] == {}


def test_build_default_client_uses_flash_by_default(tmp_store, monkeypatch):
    import server
    from core import config

    monkeypatch.setattr(config, "API_KEY", "sk-test")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    client = server.build_default_client()
    assert client.model == "deepseek-v4-flash"

    monkeypatch.setenv("AGENT_MODEL", "deepseek-v4-pro")
    assert server.build_default_client().model == "deepseek-v4-pro"


def test_build_default_client_without_key(tmp_store, monkeypatch):
    import server
    from core import config

    monkeypatch.setattr(config, "API_KEY", "")
    assert server.build_default_client() is None


def test_registry_actions_empty_when_offline():
    import server

    reg = server.Registry(timeout=0.0)
    reg.declare("sims4", [{"name": "cook", "desc": "做饭", "params": {}}])
    assert reg.actions("sims4") == []
    assert reg.actions(None) == []

    reg2 = server.Registry(timeout=60.0)
    reg2.declare("sims4", [{"name": "cook", "desc": "做饭", "params": {}}])
    assert reg2.actions("sims4")[0]["name"] == "cook"
    assert reg2.actions(None)[0]["name"] == "cook"          # 只有一个在线 mod → 用它


def test_tools_module_whitelist_matches_server():
    """server 用 tools 的白名单判定提议，不另立一套。"""
    assert tools.is_action_tool("cook", CAPS["actions"]) is True
    assert tools.is_action_tool("dance", CAPS["actions"]) is False
