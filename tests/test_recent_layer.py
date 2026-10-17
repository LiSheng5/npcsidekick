"""近况层（任务 5）——【最近发生】块：最近动作结果 + 进行中 / 待确认。

背景：任务书一之后超时动作登记 pending 而不写失败卡，若近况层不注入它，
下一回合 NPC 对这件事一无所知，很可能重复下达同一动作。

三部分（措辞先用最朴素的中文）：
  1. 最近动作结果：category == "action" 的记忆条目，最近 N 条（NPC_RECENT_RESULTS=5）
     且在最近 M 分钟内（NPC_RECENT_MINUTES=30）
  2. 进行中：pending 里 mod 已受理（state=running）且未过期 —— 「…— 进行中，已 X 秒」
  3. 待确认：同步等待超时、至今没回报（state=unknown）—— 「…— 没收到回应，
     不确定是否已做成，已 X 秒」
两类分开写；截断优先级 = 观测 > 待确认 / 进行中 > 最近结果（需确认）；
过期清理（先写「没做成: 一直没等到结果」卡）在 /api/talk 开始时已完成，
近况层只读 pending.alive()，不会把已过期条目显示成进行中。
"""
from __future__ import annotations

import time

import memory
import turn
from test_server import CAPS, FakeExecutor, kinds, text_of


# ── 超时动作 → 「待确认」，且不写失败卡 ────────────────────

def test_timeout_action_lands_in_unknown_not_failure_card(tmp_store, write_persona,
                                                          make_app_client, make_provider,
                                                          monkeypatch):
    """动作超时后，下一回合第一次 LLM 调用的上下文里就有「待确认」那条，
    同时记忆卡里没有失败卡。"""
    write_persona("cang")
    monkeypatch.setenv("NPC_EXECUTE_TIMEOUT", "0.05")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "稍等。"},
        {"text": "嗯。"},
    ])
    executor = FakeExecutor(delay=0.5)
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})
    assert memory.load_card("cang", store_dir=tmp_store) == []     # 超时 ≠ 没做成

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "好了没"})

    assert kinds(res)[-1] == "done"
    user = provider.stream_calls[-1]["messages"][-1]["content"]   # 下一回合第一次 LLM 调用
    assert "【最近发生】" in user
    assert "待确认：" in user
    assert "cook(dish=面) — 没收到回应，不确定是否已做成，已" in user
    assert "进行中" not in user.split("【最近发生】")[1]   # 超时未知 ≠ 进行中
    assert memory.load_card("cang", store_dir=tmp_store) == []     # 依旧没有失败卡
    assert "没做成" not in user


# ── running → 「进行中」；回报到达 → 「最近结果」 ──────────

def test_running_moves_to_results_after_report(tmp_store, write_persona,
                                               make_app_client, make_provider):
    """running 动作出现在「进行中」；回报到达后，下一回合它出现在「最近结果」，
    不再出现在进行中。"""
    write_persona("cang")
    provider = make_provider(turns=[
        {"text": "", "tool_calls": [{"name": "cook", "arguments": {"dish": "面"}}]},
        {"text": "火还没关，再等会儿。"},
        {"text": "嗯。"},
        {"text": "好。"},
    ])
    executor = FakeExecutor(result={"ok": False, "status": "running", "note": "还在煮"})
    client = make_app_client(provider=provider, executor=executor)
    client.post("/api/capabilities", json=CAPS)

    client.post("/api/talk", json={"npc_id": "cang", "message": "做饭", "mod": "mygame"})
    assert len(client.app.state.pending) == 1

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "好了没"})

    assert kinds(res)[-1] == "done"
    user = provider.stream_calls[-1]["messages"][-1]["content"]
    assert "进行中：" in user
    assert "cook(dish=面) — 进行中，已" in user
    assert "待确认" not in user                        # running 不是"待确认"

    call_id = executor.calls[0]["payload"]["call_id"]
    client.post("/api/action_result", json={
        "npc_id": "cang", "action": "cook", "ok": True, "note": "面煮好了",
        "call_id": call_id, "mod": "mygame"})

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "成了吗"})

    assert kinds(res)[-1] == "done"
    user = provider.stream_calls[-1]["messages"][-1]["content"]
    assert "最近动作结果：" in user
    assert "完成: 面煮好了" in user                    # 回报写卡后进「最近结果」
    assert "进行中" not in user                       # 已销账，不再"进行中"


# ── TTL 过期 → 「没做成: 一直没等到结果」，离开「待确认」 ──

def test_expired_pending_becomes_failure_card_not_unknown(tmp_store, write_persona,
                                                          make_app_client, make_provider,
                                                          monkeypatch):
    """TTL 过期后出现「没做成: 一直没等到结果」，且不再出现在待确认。"""
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

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "好了没"})   # 开头顺手 expire

    assert kinds(res)[-1] == "done"
    user = provider.stream_calls[-1]["messages"][-1]["content"]
    assert "最近动作结果：" in user
    assert "没做成: 一直没等到结果" in user            # 过期 → 失败卡
    assert "待确认" not in user                       # 已出 pending，不再"待确认"
    assert [e["content"] for e in memory.load_card("cang", store_dir=tmp_store)] == [
        "没做成: 一直没等到结果"]


# ── 截断优先级 + 日志 ─────────────────────────────────────

def test_recent_truncation_priority_and_log(tmp_store, write_persona, make_app_client,
                                            make_provider, monkeypatch, caplog):
    """条目很多、共享预算不够时：最近结果先砍光，待确认/进行中留下，
    带「…（已截断）」标记并记 context_section_truncated 日志。"""
    import logging

    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("NPC_BUDGET_STATE", "60")
    for i in range(3):
        memory.add_action_result("cang", "cook", True, f"第{i}次的大动静" * 20,
                                 store_dir=tmp_store)
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)
    client.app.state.pending.register("c1", "cang", "cook", {"dish": "面"}, "mygame",
                                      state="unknown")

    res = client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    assert kinds(res)[-1] == "done"
    user = provider.stream_calls[0]["messages"][-1]["content"]
    assert "【最近发生】" in user
    assert "待确认：" in user and "没收到回应" in user   # 待确认最后才砍
    assert "最近动作结果：" not in user                  # 最近结果先被砍光（标题随之消失）
    assert "…（已截断）" in user
    assert "context_section_truncated" in caplog.text


# ── 最近动作结果的 N / M 窗口（**需确认**默认 5 / 30）─────

def test_recent_results_defaults_and_env(monkeypatch):
    assert turn.recent_results_limit() == 5
    assert turn.recent_results_minutes() == 30.0
    monkeypatch.setenv("NPC_RECENT_RESULTS", "2")
    monkeypatch.setenv("NPC_RECENT_MINUTES", "10")
    assert turn.recent_results_limit() == 2
    assert turn.recent_results_minutes() == 10.0
    monkeypatch.setenv("NPC_RECENT_RESULTS", "-3")
    assert turn.recent_results_limit() == 0           # 负数钳到 0（等于不出结果组）


def test_recent_results_respects_time_window(tmp_store, write_persona, make_app_client,
                                             make_provider):
    """超过 M 分钟的旧动作卡不进近况层。"""
    memory.save_card("cang", [
        {"id": "old", "content": "完成: 很久以前的事", "importance": 6,
         "category": "action", "created_at": time.time() - 3600}], store_dir=tmp_store)
    write_persona("cang")
    provider = make_provider(turns=[{"text": "嗯。"}])
    client = make_app_client(provider=provider)

    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    user = provider.stream_calls[0]["messages"][-1]["content"]
    assert "很久以前的事" not in user                  # 出了 30 分钟窗口
    assert "【最近发生】" not in user                  # 没别的近况 → 整块省略


def test_params_summary_truncated_to_60_chars():
    summary = turn._params_summary({"detail": "很长的参数值" * 30})
    assert len(summary) <= 61                         # 约 60 字符 + 省略号
    assert summary.endswith("…")
    assert turn._params_summary({}) == ""
    assert turn._pending_label("cook", {"dish": "面"}) == "cook(dish=面)"
    assert turn._pending_label("cook", {}) == "cook"
