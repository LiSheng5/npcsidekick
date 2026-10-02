"""server.py 测试 —— 游戏面四端点的契约行为（全零网络）。"""
from __future__ import annotations

import asyncio
import json

import pytest

import memory
import tools


CAPS = {
    "mod": "mygame",
    "execute_url": "http://127.0.0.1:8766/execute",
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


# ── /api/capabilities（mod 报到：声明动作 + 心跳）─────────

def test_capabilities_ok(tmp_store, write_persona, make_app_client):
    client = make_app_client()

    res = client.post("/api/capabilities", json=CAPS)

    assert res.status_code == 200
    assert res.json() == {"ok": True, "mod": "mygame", "actions": ["cook", "goto", "chat"]}
    assert client.get("/api/state").json()["mods"]["mygame"]["actions"] == 3


@pytest.mark.parametrize("bad", [
    {},
    {"mod": "mygame"},
    {"mod": "", "actions": CAPS["actions"]},
    {"mod": "mygame", "actions": []},
    {"mod": "mygame", "actions": "不是数组"},
    {"mod": "mygame", "actions": [{"desc": "缺名字"}]},
    {"mod": "mygame", "actions": [{"name": "cook"}]},
    {"mod": "mygame", "actions": [{"name": "cook", "desc": "x", "params": "不是对象"}]},
])
def test_capabilities_invalid_400(tmp_store, write_persona, make_app_client, bad):
    client = make_app_client()
    assert client.post("/api/capabilities", json=bad).status_code == 400


def test_malformed_json_body_400(tmp_store, write_persona, make_app_client):
    client = make_app_client()
    res = client.post("/api/capabilities", content=b"{bad json",
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 400


# ── /api/talk：纯聊天（只出 delta 帧）─────────────────────

def test_talk_plain_text(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "行啊，我去给你煮碗面。"}])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "你能帮我做顿饭吗"})

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert kinds(res)[-1] == "done" and set(kinds(res)) == {"delta", "done"}
    assert text_of(res) == "行啊，我去给你煮碗面。"
    # 台词进了持久聊天记录（每轮两行：用户一行 + 助手一行）
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
    assert "台词" in system["content"]                     # 台词纪律（只输出角色说的话）进了提示词


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


def test_talk_uses_persona_synonyms_without_touching_global(tmp_store, write_persona,
                                                           make_app_client, make_provider):
    """同义词族按角色卡**显式传参**：本次 talk 既不写、也不读进程级全局表。

    多 NPC 并发对话时，A 的同义词表不会污染 B 的检索（回归：以前 server 每请求
    `memory.set_synonyms(...)` 写全局表，而 run_tool 没传 synonyms，落到全局回退）。
    """
    write_persona("cang", entity_synonyms={"木材": ["木材", "柴", "木头"]})
    memory.add_entry("cang", "存着过冬的木材", importance=5)
    memory.add_entry("cang", "灶边的石头", importance=5)
    memory.set_synonyms({"石头": ["石头", "柴"]})   # 别人留下的全局表：会把「柴」算到石头上
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "recall", "arguments": {"query": "柴"}}]},
        {"text": "木头在棚里。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "柴呢"})

    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"].splitlines()[0] == "- 存着过冬的木材"   # 用的是角色卡里的表
    assert memory._ACTIVE_SYNONYMS == {"石头": frozenset({"石头", "柴"})}   # 全局表原样没被动过
    memory.set_synonyms(None)
    assert kinds(res)[-1] == "done"


# ── /api/talk：动作 = LLM 直接调用的工具（大脑同步转发 mod 执行）──

class FakeExecutor:
    """假执行器：记录每次调用，按脚本返回（results 队列优先）或抛异常。"""

    def __init__(self, result=None, results=None, error=None, delay=0.0):
        self.calls = []
        self.results = list(results) if results else None
        self.result = result or {"ok": True, "status": "done", "note": "面煮好了"}
        self.error = error
        self.delay = delay

    async def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return dict(self.result)


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
    assert frames(res)[-2]["action"] == {"name": "cook", "params": {"dish": "面"}}
    assert text_of(res) == "行啊，面好了，趁热吃。"
    # mod 真的被同步调用了，载荷符合协议 §3
    assert executor.calls == [{
        "url": CAPS["execute_url"], "timeout": 30.0,
        "payload": {"npc_id": "cang", "action": "cook",
                    "params": {"dish": "面"}, "mod": "mygame"}}]
    # 结果当场写卡 + 第二轮上下文里带上工具结果
    assert [e["content"] for e in memory.load_card("cang")] == ["完成: 面煮好了"]
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

    assert [e["content"] for e in memory.load_card("cang")] == [
        "没做成: 执行接口没接上（RuntimeError）"]
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith("没做成")
    assert text_of(res) == "灶好像坏了，没做成。"


def test_talk_action_timeout_is_failure(tmp_store, write_persona, make_app_client,
                                        make_provider, monkeypatch):
    """等待超过 NPC_EXECUTE_TIMEOUT = 失败，不假装做成（协议 §3）。"""
    write_persona("cang")
    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "0.05")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "灶坏了。"},
    ])
    executor = FakeExecutor(delay=0.5)
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})

    assert executor.calls[0]["timeout"] == 0.05
    assert memory.load_card("cang")[0]["content"].startswith("没做成: 等待超过 0.05s")


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

    assert memory.load_card("cang") == []                      # 没定论就不写卡
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert "还在进行中" in tool_msg["content"]
    assert kinds(res).count("action") == 1                     # 调过就有记录


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
    assert [e["content"] for e in memory.load_card("cang")] == [
        "完成: 面煮好了", "完成: 走到厨房了"]


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

    assert [e["content"] for e in memory.load_card("cang")] == [
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


def test_http_execute_real_roundtrip():
    """默认执行器走一条真的本地 HTTP（零外网）：POST JSON → 解析 JSON 响应。"""
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


# ── /api/talk：LLM 不可用降级（回退角色卡规则回复）────────

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


# ── /api/action_result（游戏回报 → 写记忆卡）──────────────

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


def test_action_result_with_mod_refreshes_heartbeat(tmp_store, write_persona, make_app_client):
    """回报结果时捎带 mod 也算一次心跳（协议 §1 / 简报 §3）。

    场景：mod 执行长动作（>60s 没说话）后回报 —— 不该被判离线，否则大脑不再调动作。
    口径：只刷新**已报到过**的 mod；不带 mod 不猜是哪一个，心跳不动。
    """
    write_persona("cang")
    client = make_app_client()
    client.post("/api/capabilities", json=CAPS)
    registry = client.app.state.registry

    def online():
        return client.get("/api/state").json()["mods"]["mygame"]["online"]

    registry._mods["mygame"]["last_seen"] = 0.0            # 伪造成很久以前的心跳 = 已离线
    assert online() is False

    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面好了", "mod": "mygame"})

    assert res.status_code == 200
    assert online() is True                               # 回报捎带 mod → 心跳刷新、重新在线

    registry._mods["mygame"]["last_seen"] = 0.0
    client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面好了"})   # 不带 mod

    assert online() is False                              # 不带 mod 不刷新（不猜是哪个 mod）


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


# ── 状态查询（/api/state、/api/npcs）─────────────────────

def test_state_endpoint(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()
    client.post("/api/capabilities", json=CAPS)

    state = client.get("/api/state").json()

    assert state["model"] == "fake-model"
    assert state["store_dir"] == str(memory.STORE_DIR)
    assert state["npcs"] == 1
    assert state["mods"]["mygame"]["online"] is True
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
    from core.personas import build_system_prompt

    prompt = build_system_prompt({
        "identity": "部落的老猎手", "personality": "寡言直接", "speech_style": "短句",
        "voice_samples": ["别追跑得快的。"], "taboos": ["烧湿柴"],
    }, {"summary": "玩家在厨房"})

    assert "部落的老猎手" in prompt and "寡言直接" in prompt and "短句" in prompt
    assert "别追跑得快的。" in prompt
    assert "你绝不会烧湿柴" in prompt
    assert "玩家在厨房" in prompt
    assert "recall" in prompt                     # 接地：涉及往事先 recall，按逐字结果回答
    assert "不知道" in prompt                     # 接地：查不到就说不知道，绝不编造


def test_system_prompt_matches_new_execution_model():
    """执行模型只有一种：LLM 调用工具执行（动作 = 工具调用）。

    V3.2 的"提议 / 回报"是旧模型，任何形式的复辟都要被这条钉住：
    - 行为层要写明动作就是工具调用（不能只说"说出来"）
    - 台词层要保留自然语言（"我去煮饭"），不许描述工具调用本身
    - 提示词里不得再出现"提议"字样
    """
    from core.personas import build_system_prompt

    prompt = build_system_prompt({"identity": "老猎手"})

    assert "工具调用" in prompt          # 行为：动作 = 工具调用，不是"只说不做"
    assert "我去煮饭" in prompt          # 台词：自然语言表达动作
    assert "提议" not in prompt


def test_reasoning_effort_env(monkeypatch):
    import server

    monkeypatch.delenv("NPC_REASONING_EFFORT", raising=False)
    assert server.reasoning_effort() is None

    monkeypatch.setenv("NPC_REASONING_EFFORT", "  ")
    assert server.reasoning_effort() is None

    monkeypatch.setenv("NPC_REASONING_EFFORT", "off")
    assert server.reasoning_effort() == "off"


# ── 事件循环不被同步 IO 堵住（回合里的阻塞调用走线程池）─────

def test_summary_llm_does_not_block_event_loop(tmp_store, write_persona, monkeypatch):
    """滚动摘要里的同步 LLM 请求必须走线程池。

    回归：build_history 以前在 SSE 生成器里直接调同步 llm.chat —— 摘要一变慢
    （几秒到几十秒），整个事件循环就被卡住，别的 NPC / 控制台全部无响应。
    """
    import asyncio
    import time

    import httpx

    import chatlog
    import server
    from core.client import LLMClient, LLMResponse
    from core.types import StreamChunk

    monkeypatch.setenv("NPC_SUMMARY_TOKEN_THRESHOLD", "1")
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "0")
    write_persona("cang")
    chatlog.append_turn("cang", "你好世界", "嗯")     # 制造超阈值的旧轮次 → 必触发摘要

    class SlowSummaryProvider:
        """stream 立刻回台词；chat（摘要走这条）故意慢，模拟真实 LLM 延迟。"""
        model_name = "slow-fake"
        delay = 0.4

        def chat(self, messages, **kwargs):
            time.sleep(self.delay)
            return LLMResponse(content="（前情摘要）", tool_calls=[],
                               finish_reason="stop", model=self.model_name, usage=None)

        async def stream(self, messages, **kwargs):
            yield StreamChunk(content="在的。", model=self.model_name)
            yield StreamChunk(finish_reason="stop", model=self.model_name)

    app = server.create_app(llm_client=LLMClient(provider=SlowSummaryProvider()))

    async def main() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            started = time.perf_counter()
            talk = asyncio.create_task(
                ac.post("/api/talk", json={"npc_id": "cang", "message": "在吗"}))
            await asyncio.sleep(0.1)          # 循环若被摘要堵住，这一觉会被一起拖长
            drift = time.perf_counter() - started - 0.1
            assert (await talk).status_code == 200
        return drift

    drift = asyncio.run(main())

    assert drift < 0.2, f"事件循环被同步摘要堵住了 {drift:.2f}s"

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
    from core.personas import rule_reply

    persona = {"rules": {"replies": {"柴": "挑干透的。"}, "fallback": "嗯。"}}
    assert rule_reply(persona, "柴火怎么选") == "挑干透的。"
    assert rule_reply(persona, "你好") == "嗯。"
    assert rule_reply({}, "你好") == "……"


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
    assert buf.finalize(0)["args"] == {}


def test_build_default_client_follows_user_config(tmp_store, monkeypatch):
    """三件套由用户自配：缺模型名/端点 → None（不偷偷用默认厂商）；配齐 → 用配置的那套。"""
    import server
    from core import config

    monkeypatch.setattr(config, "api_key", lambda: "sk-test")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("NPC_MODEL", raising=False)
    monkeypatch.delenv("NPC_BASE_URL", raising=False)
    assert server.build_default_client() is None

    monkeypatch.setenv("AGENT_MODEL", "qwen-plus")
    monkeypatch.setenv("NPC_BASE_URL", "https://dashscope.example/v1")
    client = server.build_default_client()
    assert client.model == "qwen-plus"
    assert client.provider.base_url == "https://dashscope.example/v1"

    # 换模型名不改端点：显式配的端点永远优先，不被厂商推断覆盖
    monkeypatch.setenv("AGENT_MODEL", "deepseek-v4-pro")
    assert server.build_default_client().provider.base_url == "https://dashscope.example/v1"


def test_build_default_client_without_key(tmp_store, monkeypatch):
    import server
    from core import config

    monkeypatch.setattr(config, "api_key", lambda: "")
    assert server.build_default_client() is None


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


def test_tools_module_whitelist_matches_server():
    """server 用 tools 的白名单判定动作，不另立一套。"""
    assert tools.is_action_tool("cook", CAPS["actions"]) is True
    assert tools.is_action_tool("dance", CAPS["actions"]) is False
