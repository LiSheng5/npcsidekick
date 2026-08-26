"""协议 v1 任务回路 M1 测试: 消费者注册表 + 任务账本 + 链式状态机 + 失败商议。

端点接线(server.py hello/task_done/state 挂载)的联调冒烟在 D 盘恢复后补
（依赖 TestClient 起全 app; 本文件全部为纯单元, 无网络无 LLM）。
"""
import time

import pytest

from npc import taskloop as tl


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """每个用例独立注册表/账本 + 固定超时参数。"""
    monkeypatch.setattr(tl, "REGISTRY", tl.ConsumerRegistry())
    monkeypatch.setattr(tl, "LEDGER", tl.TaskLedger())
    monkeypatch.setenv("NPC_CONSUMER_TTL", "60")
    monkeypatch.setenv("NPC_TASK_TIMEOUT", "300")
    yield


# ── 消费者注册表 ─────────────────────────────────────────────

def test_hello_registers_and_normalizes():
    out = tl.REGISTRY.hello("shvdn_mod", "1.7", ["goto", " goto ", "", None])
    assert out["ok"] is True
    assert out["verbs"] == ["goto"]                      # 去重/去空白/剔非法
    assert tl.REGISTRY.alive()["shvdn_mod"]["version"] == "1.7"


def test_hello_requires_name():
    assert tl.REGISTRY.hello("", "1.0", ["goto"])["ok"] is False


def test_consumer_expiry_after_ttl(monkeypatch):
    tl.REGISTRY.hello("mod", "1.0", ["goto"])
    # 心跳过期: 把 last_seen 拨回 61s 前
    tl.REGISTRY._consumers["mod"]["last_seen"] = time.monotonic() - 61
    assert tl.REGISTRY.alive() == {}
    assert tl.REGISTRY.effective_verbs(["goto"]) == frozenset()


def test_effective_verbs_is_intersection(monkeypatch):
    tl.REGISTRY.hello("gta_mod", "1.7", ["follow_player", "goto", "fly_to_moon"])
    got = tl.REGISTRY.effective_verbs({"goto": {}, "gather": {}})
    assert got == frozenset({"goto"})                    # ∩ manifest; fly 不在 manifest


def test_no_consumer_means_empty_whitelist():
    assert tl.REGISTRY.effective_verbs({"goto": {}}) == frozenset()


def test_action_allowed_gate():
    manifest = {"goto": {}, "say": {}}
    assert tl.action_allowed("goto", manifest) is False   # 没消费者 → 一律拒绝
    tl.REGISTRY.hello("mod", "1.0", ["goto"])
    assert tl.action_allowed("goto", manifest) is True
    assert tl.action_allowed("drive_to", manifest) is False  # 不在 manifest


# ── 账本: 单任务生命周期 ─────────────────────────────────────

def test_book_dispatch_complete_happy_path():
    t = tl.LEDGER.book("amanda", "goto", {"place": "机场"})
    assert t["state"] == "booked"
    view = tl.LEDGER.dispatch_view()                     # 下发即标记
    assert view and view[0]["task_id"] == t["task_id"]
    assert view[0]["state"] == "dispatched"
    out = tl.LEDGER.settle(t["task_id"], "completed")
    assert out["state"] == "completed"
    assert tl.LEDGER.dispatch_view() == []               # 终态不再下发


def test_settle_rejects_unknown_and_double_settle():
    assert tl.LEDGER.settle("t_nope", "completed") is None
    t = tl.LEDGER.book("a", "goto")
    tl.LEDGER.dispatch_view()
    assert tl.LEDGER.settle(t["task_id"], "completed") is not None
    assert tl.LEDGER.settle(t["task_id"], "failed") is None   # 终态不可再销账


def test_fail_creates_discussion_for_player_talk():
    t = tl.LEDGER.book("amanda", "drive_to", {"place": "机场"}, desc="开车去机场")
    tl.LEDGER.dispatch_view()
    tl.LEDGER.settle(t["task_id"], "failed", detail="路被堵死")
    d = tl.LEDGER.discussions("amanda")
    assert len(d) == 1
    assert "没办成" in d[0]["text"] and "路被堵死" in d[0]["text"]
    assert tl.LEDGER.pop_discussions("amanda")[0]["action"] == "drive_to"
    assert tl.LEDGER.discussions("amanda") == []          # 取走即清


def test_zombie_timeout_reap_enters_negotiation(monkeypatch):
    t = tl.LEDGER.book("jimmy", "goto")
    tl.LEDGER.dispatch_view()
    # 拨钟: dispatched_at 回拨 301s
    tid = t["task_id"]
    tl.LEDGER._tasks[tid]["dispatched_at"] -= 301
    reaped = tl.LEDGER.reap_zombies()
    assert reaped == [tid]
    assert tl.LEDGER._tasks[tid]["state"] == "failed"
    assert len(tl.LEDGER.discussions("jimmy")) == 1       # 僵尸账也走商议
    fresh = tl.LEDGER.book("jimmy", "goto")
    tl.LEDGER.dispatch_view()
    assert tl.LEDGER.reap_zombies() == []                 # 新账不受牵连


def test_new_booking_supersedes_old_continuous_task():
    t1 = tl.LEDGER.book("tracey", "wander")
    tl.LEDGER.dispatch_view()
    t2 = tl.LEDGER.book("tracey", "goto", {"place": "酒吧"})
    states = {tl.LEDGER._tasks[t1["task_id"]]["state"],
              tl.LEDGER._tasks[t2["task_id"]]["state"]}
    assert tl.LEDGER._tasks[t1["task_id"]]["error"] == "superseded"


# ── 链式: 顺序派发 / 整链取消 ────────────────────────────────

def _book_chain(npc="amanda", actions=("enter_car_with_player", "drive_to")):
    ids = []
    for a in actions:
        t = tl.LEDGER.book(npc, a, chain_id="c1")
        ids.append(t["task_id"])
    return ids


def test_chain_dispatches_in_order():
    i1, i2 = _book_chain()
    view = tl.LEDGER.dispatch_view()
    assert [v["task_id"] for v in view] == [i1]           # 只派第一节
    tl.LEDGER.settle(i1, "completed")
    view = tl.LEDGER.dispatch_view()
    assert [v["task_id"] for v in view] == [i2]           # 前节 completed 才派下一节


def test_chain_failure_cancels_remaining_and_notifies():
    i1, i2 = _book_chain()
    tl.LEDGER.dispatch_view()
    tl.LEDGER.settle(i1, "failed", detail="车门打不开")
    assert tl.LEDGER._tasks[i2]["state"] == "cancelled"
    assert tl.LEDGER._tasks[i2]["error"] == "chain_failed"
    assert len(tl.LEDGER.discussions("amanda")) == 1      # 商议只记失败源一次


def test_fight_superseded_by_new_command():
    """拍板语义"新命令打断": 斗殴中下达新指令 → 旧斗殴 cancelled(superseded)。"""
    t1 = tl.LEDGER.book("amanda", "fight", {"target": "红衣服混混"})
    tl.LEDGER.dispatch_view()
    t2 = tl.LEDGER.book("amanda", "goto", {"place": "酒吧"})
    tl.LEDGER.dispatch_view()
    assert tl.LEDGER._tasks[t1["task_id"]]["state"] == "cancelled"
    assert tl.LEDGER._tasks[t1["task_id"]]["error"] == "superseded"


def test_fight_booked_through_gate_when_consumer_declares():
    """消费者声明了 fight 才能过能力门; 没声明则拒之门外。"""
    manifest = {"fight": {"tier": 3, "approval": "ask"}}
    assert tl.action_allowed("fight", manifest) is False     # 无人报到
    tl.REGISTRY.hello("gta_mod", "1.7", ["follow_player", "goto", "fight"])
    assert tl.action_allowed("fight", manifest) is True


def test_stats_snapshot_shape():
    tl.LEDGER.book("a", "goto")
    s = tl.LEDGER.stats()
    assert s["total"] == 1 and s["by_state"]["booked"] == 1
    assert s["pending_discussions"] == 0
