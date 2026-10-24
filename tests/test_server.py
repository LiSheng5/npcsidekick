"""server.py 测试（游戏面主链路）—— 报到校验 / talk 基础与装配 / 记忆工具循环 / 降级。

按端点拆分后的分工：动作执行与工具轮 → test_server_actions.py；
动作结果回报与 pending 对账 → test_server_action_result.py；
/api/state、/api/npcs 与大脑装配 → test_server_state.py。"""

from __future__ import annotations

import pytest

import chatlog
import memory
from conftest import CAPS, BoomProvider, kinds, text_of

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

def test_capabilities_execute_url_must_be_http(tmp_store, write_persona, make_app_client):
    """execute_url 是大脑要 POST 的地址：非 http(s) scheme 一律不收（SSRF 面收紧）。"""
    client = make_app_client()
    for bad in ("file:///etc/passwd", "ftp://x/execute", "http://", "javascript:alert(1)"):
        assert client.post("/api/capabilities",
                           json=dict(CAPS, execute_url=bad)).status_code == 400, bad
    assert client.post("/api/capabilities",
                       json=dict(CAPS, execute_url="http://127.0.0.1:8766/execute")
                       ).status_code == 200
    assert client.post("/api/capabilities",
                       json=dict(CAPS, execute_url="https://mod.example/execute")
                       ).status_code == 200
    assert client.post("/api/capabilities",
                       json=dict(CAPS, execute_url="")).status_code == 200      # 空 = 没给

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
    assert [m["content"] for m in chatlog.load_turns(
        "cang", store_dir=tmp_store)] == [
        "你能帮我做顿饭吗", "行啊，我去给你煮碗面。"]

def test_talk_observation_reaches_prompt(tmp_store, write_persona, make_app_client, make_provider):
    """观测不进 system：system 只放人设+规矩（固定层，利于前缀缓存），
    观测进最后一条 user 消息的【当前情况】块。"""
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    client.post("/api/talk", json={
        "npc_id": "cang", "message": "在吗",
        "observation": {"summary": "玩家在厨房，刚下班", "nearby": ["玩家", "冰箱"]}})

    messages = provider.stream_calls[0]["messages"]
    system = messages[0]
    assert system["role"] == "system"
    assert "玩家在厨房，刚下班" not in system["content"]   # 观测不在 system（固定层）
    assert "台词" in system["content"]                     # 台词纪律（只输出角色说的话）进了提示词
    user = messages[-1]
    assert user["role"] == "user"
    assert "玩家在厨房，刚下班" in user["content"]          # 观测进了 user 前缀块
    assert user["content"].endswith("玩家说：在吗")

def test_talk_oversized_observation_truncated_with_log(tmp_store, write_persona,
                                                       make_app_client, make_provider,
                                                       monkeypatch, caplog):
    """超长观测按 NPC_BUDGET_STATE 截断、尾部带标记，并记 context_section_truncated 日志。"""
    import logging

    monkeypatch.setenv("NPC_BUDGET_STATE", "20")
    caplog.set_level(logging.WARNING)
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    client.post("/api/talk", json={
        "npc_id": "cang", "message": "在吗",
        "observation": {"summary": "观" * 300}})

    user = provider.stream_calls[0]["messages"][-1]["content"]
    assert "…（已截断）" in user                           # 截断留标记，不静默丢弃
    assert user.endswith("玩家说：在吗")
    assert "观" * 300 not in user                          # 超预算部分真的被截掉了
    assert "context_section_truncated" in caplog.text      # 带层名的日志

def test_talk_chatlog_stores_raw_message_without_prefix(tmp_store, write_persona,
                                                        make_app_client, make_provider):
    """资料块只是 LLM 上下文的前缀，聊天记录里仍只存玩家原话。"""
    import chatlog

    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    client.post("/api/talk", json={
        "npc_id": "cang", "message": "在吗",
        "observation": {"summary": "玩家在厨房"}})

    turns = chatlog.load_turns("cang", store_dir=tmp_store)
    assert [m["content"] for m in turns] == ["在吗", "嗯。"]  # 玩家原话，无前缀
    assert "【当前情况】" not in turns[0]["content"]
    assert "玩家说：" not in turns[0]["content"]

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

def test_talk_rejects_traversal_npc_id(tmp_store, write_persona, make_app_client):
    """npc_id 会拼进文件名：穿越企图一律 400，不许读写 store/ 之外的文件。"""
    write_persona("cang")
    client = make_app_client()

    assert client.post("/api/talk",
                       json={"npc_id": "../store/llm_config", "message": "在吗"}
                       ).status_code == 400
    assert client.post("/api/action_result",
                       json={"npc_id": "cang/../x", "action": "cook", "ok": True}
                       ).status_code == 400

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
    entries = memory.load_card("cang", store_dir=tmp_store)
    assert [e["content"] for e in entries] == ["玩家爱吃面"]      # remember 真的落卡
    assert len(provider.stream_calls) == 2
    # 第二轮上下文里带上了工具结果
    roles = [m["role"] for m in provider.stream_calls[1]["messages"]]
    assert roles[-1] == "tool"

def test_talk_recall_loop(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    memory.add_entry("cang", "玩家上次说爱吃面", importance=8, store_dir=tmp_store)
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "recall", "arguments": {"query": "面"}}]},
        {"text": "你上次说过爱吃面。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "我上次说爱吃啥"})

    assert text_of(res) == "你上次说过爱吃面。"
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert "玩家上次说爱吃面" in tool_msg["content"]              # 逐字记忆进了上下文

def test_talk_uses_persona_synonyms_explicitly(tmp_store, write_persona,
                                               make_app_client, make_provider):
    """同义词族按角色卡**显式传参**进检索 —— 没有进程级全局表可依赖。

    多 NPC 并发对话时，A 的同义词表不会污染 B 的检索：除了本次请求传进来的表，
    检索不看任何别处（进程级全局同义词表 `memory.set_synonyms` 已删）。
    """
    write_persona("cang", entity_synonyms={"木材": ["木材", "柴", "木头"]})
    wood = memory.add_entry("cang", "存着过冬的木材", importance=5, store_dir=tmp_store)
    memory.add_entry("cang", "灶边的石头", importance=5, store_dir=tmp_store)
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "recall", "arguments": {"query": "柴"}}]},
        {"text": "木头在棚里。"},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "柴呢"})

    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"] == f"- [{wood['id'][:6]}] 存着过冬的木材"   # 用的是角色卡里的表
    assert not hasattr(memory, "_ACTIVE_SYNONYMS")    # 进程级全局表已不存在
    assert kinds(res)[-1] == "done"

def test_talk_bad_args_on_memory_tool_keeps_missing_arg_error(tmp_store, write_persona,
                                                              make_app_client, make_provider):
    """记忆工具参数坏 JSON：行为不变 —— 照旧执行，落在自己的缺参提示上（不写卡）。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "remember", "raw_arguments": "{坏"}]},
        {"text": "没记住，再说一遍。"},
    ])
    client = make_app_client(provider=provider)

    client.post("/api/talk", json={"npc_id": "cang", "message": "记住点事"})

    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "缺少 content" in tool_msg["content"]
    assert memory.load_card("cang", store_dir=tmp_store) == []


# ── /api/talk：LLM 不可用降级（回退角色卡规则回复）────────

def test_talk_falls_back_to_persona_rules(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client(provider=BoomProvider())

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "今天说说狩猎的事"})

    assert res.status_code == 200
    assert kinds(res) == ["delta", "done"]
    assert text_of(res) == "别追跑得快的。"          # 命中角色卡 rules.replies 的关键词
    assert "action" not in kinds(res)

def test_talk_fallback_uses_default_when_no_keyword(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client(provider=BoomProvider())

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "随便聊聊"})

    assert text_of(res) == "嗯，火塘边坐着说。"


# ── 提示词 / 规则回复 / 思考档位（单元）──────────────────

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

    monkeypatch.setenv("NPC_REASONING_EFFORT", " high ")   # 空白剥掉
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
