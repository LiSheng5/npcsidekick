"""POST/GET /api/events 测试 + 玩家行为库的 FakeProvider 端到端链路。

端到端验收口径（任务书 §11.1）：先 POST Sims 4 事件 → /api/talk 的提示词带
【玩家对你做了什么】与【玩家行为记录】→ 模型调用 check_player_log 后再出台词
→ 完整成功的回合把送进提示词的新事标记已读。
"""
from __future__ import annotations

import json
import time

import pytest

import events
from conftest import CAPS, BoomProvider, FakeExecutor, frames, kinds, text_of


def hug_event(npc_id="cang", **kw):
    ev = {"kind": "to_npc", "npc_id": npc_id, "type": "hug_started",
          "label": "发起拥抱", "summary": "玩家对你发起了拥抱互动",
          "severity": 2, "merge_key": "sim_Hug:101:202",
          "game_time": "16:25:11.040 day:0 week:1",
          "detail": {"affordance": "sim_Hug", "phase": "started",
                     "detection": "si_poll", "actor_sim_id": "101",
                     "target_sim_id": "202"}}
    ev.update(kw)
    return ev


def cook_event(**kw):
    ev = {"kind": "by_player", "type": "cook_started", "label": "开始做饭",
          "summary": "玩家开始做饭", "severity": 1,
          "merge_key": "cook:101:obj1", "witnesses": ["cang"],
          "visibility": "witnessed", "location": "柳溪镇家中",
          "game_time": "16:24:00 day:0 week:1",
          "detail": {"affordance": "Cooking", "phase": "started"}}
    ev.update(kw)
    return ev


SIMS4_PAYLOAD = {"mod": "sims4", "events": [hug_event(), cook_event()]}


def last_user_content(provider, call: int = 0) -> str:
    """取第 call 次 stream 调用里的最后一条 user 消息。

    turn.py 传给 llm.stream 的 messages 列表在回合中会被追加（assistant / tool），
    同一对象引用在事后看时 [-1] 已不是当时的 user 消息 —— 按角色过滤才稳。
    """
    return [m for m in provider.stream_calls[call]["messages"]
            if m["role"] == "user"][-1]["content"]


# ── POST /api/events ────────────────────────────────────

def test_post_events_ok(tmp_store, write_persona, make_app_client):
    client = make_app_client()

    res = client.post("/api/events", json=SIMS4_PAYLOAD)

    assert res.status_code == 200
    assert res.json() == {"ok": True, "accepted": 2, "merged": 0}
    to_npc_rows = client.get("/api/events", params={"kind": "to_npc"}).json()["events"]
    assert len(to_npc_rows) == 1 and to_npc_rows[0]["type"] == "hug_started"
    assert to_npc_rows[0]["label"] == "发起拥抱"
    assert to_npc_rows[0]["delivered_at"] is None
    by_rows = client.get("/api/events", params={"kind": "by_player"}).json()["events"]
    assert len(by_rows) == 1 and by_rows[0]["location"] == "柳溪镇家中"


def test_post_events_merge_counted(tmp_store, make_app_client):
    client = make_app_client()
    client.post("/api/events", json=SIMS4_PAYLOAD)

    res = client.post("/api/events", json=SIMS4_PAYLOAD)   # 同键再投 → 全部合并

    assert res.json() == {"ok": True, "accepted": 2, "merged": 2}
    rows = client.get("/api/events", params={"kind": "to_npc"}).json()["events"]
    assert len(rows) == 1 and rows[0]["count"] == 2


@pytest.mark.parametrize("bad, field_error", [
    ({}, False),                                                # 缺 events
    ({"events": "不是数组"}, False),
    ({"events": []}, False),                                    # 空批次
    ({"events": [hug_event()] * 51}, False),                    # 超过 50 条
    ({"mod": 123, "events": [hug_event()]}, False),             # mod 类型错
    ({"events": [{"kind": "to_npc"}]}, True),                   # 缺 npc_id/type/summary
    ({"events": [dict(hug_event(), summary="")]}, True),
    ({"events": [dict(hug_event(), summary="x" * 201)]}, True),
    ({"events": [dict(hug_event(), severity=11)]}, True),
    ({"events": [dict(hug_event(), label="超过八个字符的名字")]}, True),
    ({"events": [dict(hug_event(), type="坏 类型")]}, True),
    ({"events": [dict(hug_event(), npc_id="../x")]}, True),
    ({"events": [dict(cook_event(), witnesses="cang")]}, True),
    ({"events": [dict(cook_event(), witnesses=["../x"])]}, True),
    ({"events": [dict(cook_event(), visibility="secret")]}, True),
    ({"events": [dict(cook_event(), detail=[1])]}, True),
    ({"events": [dict(cook_event(), detail={"blob": "x" * 5000})]}, True),
    ({"events": [dict(cook_event(), game_time=123)]}, True),
])
def test_post_events_invalid_400_no_write(tmp_store, make_app_client, bad, field_error):
    """任何一条非法 → 整批不落库（400，detail 指明 events[i].字段）。"""
    client = make_app_client()
    good = {"kind": "to_npc", "npc_id": "cang", "type": "chat_started",
            "summary": "玩家对你发起寒暄"}

    payload = dict(bad)
    if field_error and isinstance(payload.get("events"), list):
        payload["events"] = [good, *payload["events"]]     # 合法第一条 + 非法第二条

    res = client.post("/api/events", json=payload)

    assert res.status_code == 400
    if field_error:
        assert "events[1]" in res.json()["detail"]          # 指明第几条哪个字段
    assert client.get("/api/events", params={"kind": "to_npc"}).json()["events"] == []


def test_post_events_db_error_returns_500(tmp_store, make_app_client, monkeypatch):
    """数据库异常 → 整批回滚 + 明确 5xx，不得假成功。"""
    client = make_app_client()

    def boom(*a, **kw):
        raise RuntimeError("disk i/o error")

    monkeypatch.setattr(events, "record_events", boom)
    res = client.post("/api/events", json=SIMS4_PAYLOAD)
    assert res.status_code == 500
    assert "写入失败" in res.json()["detail"]


def test_post_events_mod_field_touches_heartbeat(tmp_store, make_app_client):
    client = make_app_client(heartbeat_timeout_override=0.5)
    client.post("/api/capabilities", json=CAPS)
    registry = client.app.state.registry
    time.sleep(0.6)                                    # 心跳超时 → 离线
    assert registry.online("mygame") is False

    client.post("/api/events", json={"mod": "mygame", "events": [hug_event()]})

    assert registry.online("mygame") is True           # POST /api/events 捎带的心跳


# ── GET /api/events（调试）─────────────────────────────

def test_get_events_requires_known_kind(tmp_store, make_app_client):
    client = make_app_client()
    for bad in (None, "", "both", "to"):
        assert client.get("/api/events", params={"kind": bad}).status_code == 400


def test_get_events_filters_and_limit(tmp_store, make_app_client):
    client = make_app_client()
    client.post("/api/events", json={"events": [
        hug_event(npc_id="cang"),
        dict(hug_event(), npc_id="bai", type="chat_started",
             merge_key="sim_Chat:101:202", summary="玩家对贝拉发起寒暄"),
        cook_event(),
        dict(cook_event(), type="secret_started", merge_key="s",
             witnesses=["bai"], summary="玩家偷偷摸摸"),
    ]})

    # to_npc + npc_id 过滤
    rows = client.get("/api/events", params={"kind": "to_npc", "npc_id": "cang"}
                      ).json()["events"]
    assert [r["npc_id"] for r in rows] == ["cang"]

    # by_player + npc_id 过滤 = 该 NPC 可见行为（看不到 bai 独享目击的 secret）
    rows = client.get("/api/events", params={"kind": "by_player", "npc_id": "cang"}
                      ).json()["events"]
    assert [r["type"] for r in rows] == ["cook_started"]

    # 不带 npc_id 的 by_player → 全量（调试口径）
    rows = client.get("/api/events", params={"kind": "by_player"}).json()["events"]
    assert {r["type"] for r in rows} == {"cook_started", "secret_started"}

    # limit 上限 200、非法值 400
    assert client.get("/api/events", params={"kind": "to_npc", "limit": "x"}
                      ).status_code == 400
    assert len(client.get("/api/events", params={"kind": "to_npc", "limit": 1}
                          ).json()["events"]) == 1


def test_get_events_rejects_traversal_npc_id(tmp_store, make_app_client):
    client = make_app_client()
    assert client.get("/api/events",
                      params={"kind": "to_npc", "npc_id": "../x"}).status_code == 400


# ── 推送进提示词 + 已读标记（turn 链路）──────────────────

def test_events_push_into_prompt_and_mark_after_success(tmp_store, write_persona,
                                                        make_app_client, make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/events", json=SIMS4_PAYLOAD)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res)[-1] == "done"
    user = last_user_content(provider)
    assert "【玩家对你做了什么】" in user                     # 新块存在
    assert "玩家对你发起了拥抱互动" in user
    assert user.index("【玩家对你做了什么】") < user.index("玩家说：")
    assert "【玩家行为记录】" in user                          # 目录行也在
    assert "想了解细节可查记录" in user
    # 成功回合 → to_npc 事件标记已读
    rows = client.get("/api/events", params={"kind": "to_npc", "npc_id": "cang"}
                      ).json()["events"]
    assert rows[0]["delivered_at"] is not None


def test_events_push_order_between_state_and_memory(tmp_store, write_persona,
                                                    make_app_client, make_provider):
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/events", json={"events": [hug_event()]})
    import memory
    memory.add_entry("cang", "玩家上次说过爱整洁", store_dir=tmp_store)

    client.post("/api/talk", json={
        "npc_id": "cang", "message": "在吗",
        "observation": {"summary": "玩家在厨房"}})

    user = last_user_content(provider)
    assert (user.index("【当前情况】") < user.index("【玩家对你做了什么】")
            < user.index("【相关记忆】") < user.index("玩家说："))


def test_events_not_marked_on_llm_fallback_then_repushed(tmp_store, write_persona,
                                                         make_app_client, make_provider):
    """降级回合不标记已读；下一回合把新事重新推送。"""
    write_persona("cang")
    client = make_app_client(provider=BoomProvider())
    client.post("/api/events", json={"events": [hug_event()]})

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})
    assert text_of(res) == "嗯，火塘边坐着说。"          # 规则回复（降级）
    rows = client.get("/api/events", params={"kind": "to_npc"}).json()["events"]
    assert rows[0]["delivered_at"] is None              # 降级回合不标已读

    provider = make_provider(turns=[{"text": "嗯。"}])
    _swap_client(client, provider)                      # 换上可用的大脑
    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})
    user = last_user_content(provider)
    assert "玩家对你发起了拥抱互动" in user              # 未读事件重推
    rows = client.get("/api/events", params={"kind": "to_npc"}).json()["events"]
    assert rows[0]["delivered_at"] is not None          # 成功回合才标记


def _swap_client(client, provider):
    """把 app 的 LLM 客户端换成包着新 provider 的（复用 LLMClient 的壳）。"""
    from core.client import LLMClient
    client.app.state.llm_holder["client"] = LLMClient(provider=provider)
    return client.app.state.llm_holder["client"]


def test_events_recent_push_after_delivered(tmp_store, write_persona, make_app_client,
                                            make_provider):
    """已读事件在近事窗口内继续以 [之前] 展示（资料块不进聊天记录，需重推近况）。"""
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}, {"text": "好。"}])
    client = make_app_client(provider=provider)
    client.post("/api/events", json={"events": [hug_event()]})

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})
    first = last_user_content(provider)
    assert "[新]" in first

    client.post("/api/talk", json={"npc_id": "cang", "message": "刚才抱我的是你吗"})
    second = last_user_content(provider, call=1)
    assert "[之前]" in second and "玩家对你发起了拥抱互动" in second


def test_events_budget_truncation_keeps_unread(tmp_store, write_persona, make_app_client,
                                               make_provider, monkeypatch, caplog):
    """预算砍掉的事件保持未读（下回合重推），并记 context_section_truncated 日志。"""
    import logging

    monkeypatch.setenv("NPC_BUDGET_EVENTS", "1")        # 极小预算 → 必砍
    caplog.set_level(logging.WARNING)
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/events", json={"events": [hug_event()]})

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res)[-1] == "done"
    user = last_user_content(provider)
    assert "【玩家对你做了什么】" not in user            # 全砍光整块省略
    assert "context_section_truncated" in caplog.text   # 截断留日志（不静默）
    rows = client.get("/api/events", params={"kind": "to_npc"}).json()["events"]
    assert rows[0]["delivered_at"] is None              # 被砍的事件保持未读


def test_events_read_failure_does_not_break_talk(tmp_store, write_persona, make_app_client,
                                                 make_provider, monkeypatch):
    """事件读取失败只告警：资料块省略，对话照常，不进 LLM 降级。"""
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/events", json={"events": [hug_event()]})

    def boom(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(events, "select_push", boom)
    monkeypatch.setattr(events, "digest_info", boom)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res) == ["delta", "done"]
    assert text_of(res) == "嗯。"                        # 正常 LLM 回复，没走规则兜底
    user = last_user_content(provider)
    assert "【玩家对你做了什么】" not in user


def test_events_mark_failure_only_warns(tmp_store, write_persona, make_app_client,
                                        make_provider, monkeypatch):
    """已读标记失败只告警，不影响 action / done 帧。"""
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.post("/api/events", json={"events": [hug_event()]})

    def boom(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(events, "mark_delivered", boom)
    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res) == ["delta", "done"]
    assert text_of(res) == "嗯。"


# ── 取消 / 部分输出后异常：不标已读（直接消费 stream_turn）────

def _stream_once(provider, persona, tmp_store):
    """跑一个完整回合（不经 HTTP），返回是否抛出异常。"""
    import asyncio

    import turn
    from core.client import LLMClient

    async def run():
        async for _ in turn.stream_turn(
                llm=LLMClient(provider=provider), npc_id="cang", message="在吗",
                persona=persona, lock=asyncio.Lock(), wants_voice=False,
                store_dir=tmp_store, execute_timeout=lambda: 1.0):
            pass

    asyncio.run(run())


def test_events_not_marked_on_partial_output_then_exception(tmp_store, write_persona,
                                                            make_provider):
    """部分台词已输出后 LLM 异常（走降级兜底）：else 分支不执行 → 不标已读。"""
    from conftest import FakeProvider
    from core.types import StreamChunk

    class BoomAfterText(FakeProvider):
        async def stream(self, messages, tools=None, **kw):
            self.stream_calls.append({"messages": messages, "tools": tools})
            yield StreamChunk(content="好", model=self.model_name, index=1)
            raise RuntimeError("boom mid-stream")

    persona = write_persona("cang")
    client_events = events.validate_events([hug_event()])
    events.record_events(client_events, store_dir=tmp_store)

    _stream_once(BoomAfterText(), persona, tmp_store)     # 降级处理，不抛

    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert rows[0]["delivered_at"] is None               # 异常回合的新事保持未读


def test_events_not_marked_on_cancelled_turn(tmp_store, write_persona):
    """取消（CancelledError 是 BaseException，不走降级分支）→ 不标已读。"""
    import asyncio

    import pytest as _pytest

    from conftest import FakeProvider
    import turn
    from core.client import LLMClient

    class Cancelled(FakeProvider):
        async def stream(self, messages, tools=None, **kw):
            self.stream_calls.append({"messages": messages, "tools": tools})
            raise asyncio.CancelledError()
            yield  # pragma: no cover

    persona = write_persona("cang")
    events.record_events(events.validate_events([hug_event()]), store_dir=tmp_store)

    async def run():
        async for _ in turn.stream_turn(
                llm=LLMClient(provider=Cancelled()), npc_id="cang", message="在吗",
                persona=persona, lock=asyncio.Lock(), wants_voice=False,
                store_dir=tmp_store, execute_timeout=lambda: 1.0):
            pass

    with _pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert rows[0]["delivered_at"] is None               # 取消回合的新事保持未读


def test_npc_player_log_off_disables_injection_but_api_alive(tmp_store, write_persona,
                                                             make_app_client, make_provider,
                                                             monkeypatch):
    """NPC_PLAYER_LOG 关：接口照收，但提示词不带事件块/目录行、不给查询工具。"""
    monkeypatch.setenv("NPC_PLAYER_LOG", "0")
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    assert client.post("/api/events", json=SIMS4_PAYLOAD).status_code == 200  # 接口照收

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})
    assert kinds(res) == ["delta", "done"]
    user = last_user_content(provider)
    assert "【玩家对你做了什么】" not in user and "【玩家行为记录】" not in user
    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert "check_player_log" not in names
    # 事件不会被标记已读（开关关 → 连推送都不做）
    rows = client.get("/api/events", params={"kind": "to_npc"}).json()["events"]
    assert rows[0]["delivered_at"] is None


# ── 端到端（任务书 §11.1 验收）─────────────────────────

def test_end_to_end_sims4_event_tool_and_reply(tmp_store, write_persona, make_app_client,
                                               make_provider):
    """端到端：POST Sims4 事件 → talk 提示词带【玩家对你做了什么】+【玩家行为记录】→
    模型调 check_player_log（看到自己目击的行为）→ 出台词 → 成功回合标记已读。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "check_player_log",
                                     "arguments": {"keyword": "做饭"}}]},
        {"text": "你刚抱了我，这就去做饭是吧。"},
    ])
    client = make_app_client(provider=provider)
    assert client.post("/api/events", json=SIMS4_PAYLOAD).status_code == 200

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "我刚才在干嘛",
                                         "mod": "sims4"})

    assert kinds(res)[-1] == "done"
    # 1) 提示词带事件块与目录行
    user = last_user_content(provider)
    assert "【玩家对你做了什么】" in user and "玩家对你发起了拥抱互动" in user
    assert "【玩家行为记录】" in user and "条玩家行为" in user
    # 2) 查询工具已提供（recall 之后、动作之前）
    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert names[:3] == ["remember", "recall", "check_player_log"]
    # 3) 工具结果进了第二轮上下文（自己目击的做饭可见）
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "玩家开始做饭" in tool_msg["content"]
    # 4) 台词出了
    assert text_of(res) == "你刚抱了我，这就去做饭是吧。"
    # 5) 成功回合把推送过的新事标记已读
    rows = client.get("/api/events", params={"kind": "to_npc", "npc_id": "cang"}
                      ).json()["events"]
    assert rows[0]["delivered_at"] is not None


def test_end_to_end_tool_query_respects_visibility(tmp_store, write_persona,
                                                   make_app_client, make_provider):
    """模型查询时看不到自己未目击且非公开的行为（SQL 层过滤）。

    cang 有自己目击的行为（工具照常提供），但按关键词查 bai 独享目击的事
    → 结果为空。
    """
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "check_player_log",
                                     "arguments": {"keyword": "偷偷"}}]},
        {"text": "没看到什么。"},
    ])
    client = make_app_client(provider=provider)
    client.post("/api/events", json={"events": [
        cook_event(),                                   # cang 目击 → 工具提供
        dict(cook_event(), merge_key="s", type="secret_started", label="悄悄",
             summary="玩家偷偷做了某事", witnesses=["bai"], visibility="witnessed"),
    ]})

    client.post("/api/talk", json={"npc_id": "cang", "message": "我刚才在干嘛"})

    names = [t["function"]["name"] for t in provider.stream_calls[0]["tools"]]
    assert "check_player_log" in names                  # 有可见行为 → 工具提供
    assert len(provider.stream_calls) == 2              # 查询正常执行（未被忽略）
    tool_msg = provider.stream_calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"] == "没有符合条件的记录"     # 非目击且非公开 → 查不到


def test_unoffered_check_player_log_hard_call_is_ignored(tmp_store, write_persona,
                                                         make_app_client, make_provider,
                                                         monkeypatch, tmp_path):
    """工具未提供（无可见行为）时模型硬调用 → 只记账 ignored，不执行、不查库。"""
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NPC_TRACE_FILE", str(trace_file))
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "check_player_log", "arguments": {}}]},
    ])
    client = make_app_client(provider=provider)

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res)[-1] == "done"
    assert len(provider.stream_calls) == 1               # 没有为被忽略的调用多跑轮次
    trace = json.loads(trace_file.read_text("utf-8").splitlines()[0])
    assert trace["tools"] == [{"name": "check_player_log", "kind": "log",
                               "ok": False, "status": "ignored", "ms": 0.0}]


def test_player_log_switch_off_hard_call_bypasses_nothing(tmp_store, write_persona,
                                                          make_app_client, make_provider,
                                                          monkeypatch):
    """NPC_PLAYER_LOG 关时模型硬调用 check_player_log → run_tool 也拒绝（双保险）。"""
    import tools as tools_mod
    from core.schema import ToolResultStatus

    monkeypatch.setenv("NPC_PLAYER_LOG", "0")
    res = tools_mod.run_tool("check_player_log", {}, "cang", store_dir=tmp_store)
    assert res.status == ToolResultStatus.ERROR
    assert "未开启" in res.error
