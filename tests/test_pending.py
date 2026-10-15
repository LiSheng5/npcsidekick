"""pending.PendingActions 单元行为 —— 精确取出 / FIFO 兜底 / TTL 清理（全内存，零网络）。"""
from __future__ import annotations

import pending


def test_new_call_id_is_unique_hex():
    first, second = pending.new_call_id(), pending.new_call_id()

    assert len(first) == 32 and int(first, 16) >= 0      # uuid4().hex
    assert first != second


def test_register_and_resolve_exact():
    table = pending.PendingActions()
    table.register("c1", "cang", "cook", {"dish": "面"}, "mygame")
    table.register("c2", "cang", "cook", {"dish": "汤"}, "mygame")

    info = table.resolve("c2")                          # 按 id 精确取，不吃 FIFO 的亏

    assert info["call_id"] == "c2" and info["params"] == {"dish": "汤"}
    assert info["npc_id"] == "cang" and info["action"] == "cook" and info["mod"] == "mygame"
    assert info["created_at"] > 0
    assert len(table) == 1
    assert table.resolve("c2") is None                  # 取过就移除（重复回报取不到）
    assert table.resolve("查无此id") is None


def test_register_copies_params_and_tolerates_bad_params():
    table = pending.PendingActions()
    params = {"dish": "面"}
    info = table.register("c1", "cang", "cook", params)
    params["dish"] = "汤"                               # 事后改原字典不该影响登记

    assert info["params"] == {"dish": "面"}
    assert table.register("c2", "cang", "cook", None)["params"] == {}


def test_resolve_legacy_takes_earliest_by_npc_and_action():
    table = pending.PendingActions()
    table.register("c1", "cang", "cook", {"dish": "面"})
    table.register("c2", "ali", "cook", {"dish": "汤"})
    table.register("c3", "cang", "cook", {"dish": "粥"})

    info = table.resolve_legacy("cang", "cook")         # 同名动作有多条 → 最早的先销账

    assert info["call_id"] == "c1"
    assert table.resolve_legacy("cang", "cook")["call_id"] == "c3"
    assert table.resolve_legacy("cang", "cook") is None
    assert table.resolve_legacy("cang", "goto") is None
    assert len(table) == 1                              # ali 那条不受影响


def test_expire_returns_over_ttl_and_keeps_fresh():
    table = pending.PendingActions(ttl=10.0)
    old = table.register("c1", "cang", "cook", {"dish": "面"})
    old["created_at"] -= 20                             # 把最早那条做旧（返回的就是表里那份）
    table.register("c2", "cang", "goto", {"place": "厨房"})

    expired = table.expire()

    assert [i["call_id"] for i in expired] == ["c1"]
    assert len(table) == 1 and table.resolve("c2") is not None


def test_expire_honours_env_ttl(monkeypatch):
    monkeypatch.setenv("NPC_PENDING_TTL", "7")
    assert pending.PendingActions().ttl() == 7.0        # 现读环境变量
    assert pending.PendingActions(ttl=2.0).ttl() == 2.0  # 构造时显式给的优先
    monkeypatch.setenv("NPC_PENDING_TTL", " 坏 ")
    assert pending.PendingActions().ttl() == pending.DEFAULT_PENDING_TTL
