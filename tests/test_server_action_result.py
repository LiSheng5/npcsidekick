"""server.py 测试 —— /api/action_result（协议 §4）：call_id 精确销账、旧 mod FIFO、
TTL 过期与迟到回报覆盖、重复投递去重。"""

from __future__ import annotations

import pytest

import memory
from conftest import CAPS, FakeExecutor

# ── /api/action_result（游戏回报 → 写记忆卡）──────────────

def test_action_result_writes_memory(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面煮好了，玩家吃了说不错"})

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert memory.load_card("cang", store_dir=tmp_store)[0]["content"] == \
        "完成: 面煮好了，玩家吃了说不错"

def test_action_result_failure_wording(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()

    client.post("/api/action_result", json={"npc_id": "cang", "action": "goto", "ok": False})

    assert memory.load_card("cang", store_dir=tmp_store)[0]["content"] == "没做成: goto"

def test_action_result_unknown_call_id_is_not_recorded(tmp_store, write_persona, make_app_client):
    """未知 / 重复 / 已过期的 call_id：不写卡（同一次动作不能写两遍），仍返回 200。"""
    write_persona("cang")
    client = make_app_client()

    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面好了",
        "call_id": "查无此id"})

    assert res.status_code == 200
    assert res.json()["matched"] is False and res.json()["entry"] is None
    assert memory.load_card("cang", store_dir=tmp_store) == []

def test_action_result_bad_call_id_type_400(tmp_store, write_persona, make_app_client):
    write_persona("cang")
    client = make_app_client()
    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "call_id": 42})
    assert res.status_code == 400

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


# ── pending 对账（call_id 精确销账 / 旧 mod FIFO / TTL 过期）──

def test_action_result_legacy_without_call_id_records_and_clears_pending_fifo(
        tmp_store, write_persona, make_app_client, make_provider):
    """旧 mod（回报不带 call_id）：照旧写卡，并按 FIFO 销掉**最早**那条登记（旧回报不能丢）。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "行。"},
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "汤"}}]},
        {"text": "行。"},
    ])
    executor = FakeExecutor(result={"ok": False, "status": "running", "note": ""})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    for message in ("做点面", "再弄碗汤"):
        client.post("/api/talk", json={"npc_id": "cang", "message": message, "mod": "mygame"})

    pending = client.app.state.pending
    assert len(pending) == 2

    res = client.post("/api/action_result", json={"npc_id": "cang", "action": "cook", "ok": True})

    assert res.json()["matched"] is True                       # 命中登记 → matched=true
    assert res.json()["entry"]["content"] == "完成: cook（dish=面）"   # FIFO：销掉最早那条
    assert len(pending) == 1

    client.post("/api/action_result", json={"npc_id": "cang", "action": "cook", "ok": False})

    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: cook（dish=面）", "没做成: cook（dish=汤）"]
    assert len(pending) == 0

def test_action_result_call_id_match_does_not_mix_same_action(tmp_store, write_persona,
                                                             make_app_client, make_provider):
    """同一 NPC 连开两个同名动作：按 call_id 精确销账，互不串（不走 FIFO）。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "行。"},
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "汤"}}]},
        {"text": "行。"},
    ])
    executor = FakeExecutor(result={"ok": False, "status": "running", "note": ""})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    for message in ("做点面", "再弄碗汤"):
        client.post("/api/talk", json={"npc_id": "cang", "message": message, "mod": "mygame"})

    pending = client.app.state.pending
    first_id = executor.calls[0]["payload"]["call_id"]
    second_id = executor.calls[1]["payload"]["call_id"]
    assert len(pending) == 2 and first_id != second_id

    # 先回报**后**开的那条：拿到的必须是它自己的参数（dish=汤），不是最早那条
    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "call_id": second_id})
    assert res.json()["entry"]["content"] == "完成: cook（dish=汤）"
    assert len(pending) == 1

    res = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "call_id": first_id})
    assert res.json()["entry"]["content"] == "完成: cook（dish=面）"
    assert len(pending) == 0

def test_pending_ttl_expiry_writes_failure_memory(tmp_store, write_persona, make_app_client,
                                                  make_provider, monkeypatch):
    """TTL 到点还没回报 → 写一条失败记忆；迟到的回报不另写一条，而是**覆盖**这张卡（协议 §4）。"""
    write_persona("cang")
    monkeypatch.setenv("NPC_PENDING_TTL", "0")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "再等等。"},
        {"text": "嗯。"},
    ])
    executor = FakeExecutor(result={"ok": False, "status": "running", "note": ""})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})
    call_id = executor.calls[0]["payload"]["call_id"]
    assert len(client.app.state.pending) == 1
    assert memory.load_card("cang", store_dir=tmp_store) == []

    client.post("/api/talk", json={"npc_id": "cang", "message": "好了没"})   # 下一轮开头顺手 expire

    assert len(client.app.state.pending) == 0
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "没做成: 一直没等到结果"]

    late = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "其实做成了",
        "call_id": call_id})

    assert late.json()["matched"] is True                      # call_id 对得上 → 真实结果来改卡
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: 其实做成了"]                                    # 覆盖失败卡，同一动作不留两条

    again = client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "其实做成了",
        "call_id": call_id})
    assert again.json()["matched"] is False                    # 只许覆盖一次，重复回报不再写卡
    assert len(memory.load_card("cang", store_dir=tmp_store)) == 1

def test_action_result_at_ttl_boundary_still_writes_success(tmp_store, write_persona,
                                                            make_app_client, make_provider,
                                                            monkeypatch):
    """TTL 边界才到的回报：先销账再清理过期 —— 成功结果照写，不被 expire 先扫成失败。"""
    write_persona("cang")
    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "0.05")
    monkeypatch.setenv("NPC_PENDING_TTL", "0")
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
        "npc_id": "cang", "action": "cook", "ok": True, "note": "其实煮好了", "call_id": call_id})

    assert res.json()["matched"] is True
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: 其实煮好了"]


# ── 旧 mod 重复回报的短窗去重 ────────────────────────────

def test_action_result_legacy_duplicate_report_writes_once(tmp_store, write_persona,
                                                           make_app_client):
    """旧 mod（无 call_id）重试投递同一份回报 → 短窗内只写一次卡，第二份回 deduped。"""
    write_persona("cang")
    client = make_app_client()
    body = {"npc_id": "cang", "action": "cook", "ok": True, "note": "面煮好了"}

    first = client.post("/api/action_result", json=body)
    again = client.post("/api/action_result", json=body)

    assert first.json()["entry"] is not None and first.json().get("deduped") is not True
    assert again.json()["deduped"] is True and again.json()["entry"] is None
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "完成: 面煮好了"]

    other = client.post("/api/action_result", json=dict(body, note="汤也煮好了"))
    assert other.json().get("deduped") is not True           # 内容不同 = 新回报，照写
    assert len(memory.load_card("cang", store_dir=tmp_store)) == 2

def test_action_result_legacy_dedup_is_time_boxed(tmp_store, write_persona, make_app_client,
                                                  monkeypatch):
    """去重只是短窗（防重试投递），窗口过了同一份回报仍能写 —— 别做成长幂等。"""
    import server

    monkeypatch.setattr(server, "LEGACY_REPORT_DEDUP_S", -1)
    write_persona("cang")
    client = make_app_client()
    body = {"npc_id": "cang", "action": "cook", "ok": True, "note": "面煮好了"}

    assert client.post("/api/action_result", json=body).json().get("deduped") is not True
    assert client.post("/api/action_result", json=body).json().get("deduped") is not True
    assert len(memory.load_card("cang", store_dir=tmp_store)) == 2

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
