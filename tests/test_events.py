"""events.py 测试 —— 存储 / 合并 / 清理 / 推送选取 / 可见查询 / 校验。

全部离线：SQLite 落 tmp_path 注入的 store_dir，零网络。时间一律锚定真实时钟
（顺手清理按真实时钟限流，人工 1970 时间戳会被当过期记录清掉）；
合并/清理窗口用注入的 now 偏移控制。
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

import events


def to_npc(npc_id: str = "cang", type_: str = "hug_started", **kw) -> dict:
    ev = {"kind": "to_npc", "npc_id": npc_id, "type": type_,
          "summary": "玩家对你发起了拥抱互动"}
    ev.update(kw)
    return ev


def by_player(type_: str = "cook_started", **kw) -> dict:
    ev = {"kind": "by_player", "type": type_, "summary": "玩家开始做饭"}
    ev.update(kw)
    return ev


def record(raw_events, store_dir, now=None):
    """校验 + 写入一步到位（测试便捷入口）。"""
    return events.record_events(events.validate_events(raw_events), store_dir=store_dir,
                                now=now)


# ── 建库 ────────────────────────────────────────────────

def test_db_created_with_wal_and_schema(tmp_store):
    accepted, merged = record([to_npc()], tmp_store)
    assert (accepted, merged) == (1, 0)
    path = events.db_path(tmp_store)
    assert path.exists() and path.name == "events.db"
    conn = sqlite3.connect(str(path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert mode.lower() == "wal"
    assert {"npc_interactions", "player_acts", "act_witnesses"} <= tables


# ── 合并规则 ────────────────────────────────────────────

def test_merge_within_window_counts_and_updates_last_ts(tmp_store):
    t0 = time.time()
    record([to_npc(merge_key="sim_Hug:1:2")], tmp_store, now=t0)
    accepted, merged = record([to_npc(merge_key="sim_Hug:1:2")], tmp_store, now=t0 + 30)
    assert (accepted, merged) == (1, 1)
    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert len(rows) == 1                              # 合并不新增行
    row = rows[0]
    assert row["count"] == 2 and row["ts"] == t0       # 首次发生不变
    assert row["last_ts"] == t0 + 30                   # 最近一次更新


def test_no_merge_outside_window(tmp_store, monkeypatch):
    monkeypatch.setenv("NPC_EVENT_MERGE_SECONDS", "60")
    t0 = time.time()
    record([to_npc(merge_key="k")], tmp_store, now=t0)
    record([to_npc(merge_key="k")], tmp_store, now=t0 + 61)   # 出窗 → 新行
    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert len(rows) == 2


def test_merge_uses_newest_fields_but_keeps_first_ts(tmp_store):
    t0 = time.time()
    record([to_npc(merge_key="k", label="发起拥抱", severity=2,
                   game_time="16:25:11 day:0 week:1", detail={"phase": "started"})],
           tmp_store, now=t0)
    record([to_npc(merge_key="k", label="再次拥抱", severity=4,
                   game_time="16:26:00 day:0 week:1", detail={"phase": "still"})],
           tmp_store, now=t0 + 10)
    row = events.list_events("to_npc", store_dir=tmp_store)[0]
    assert row["label"] == "再次拥抱" and row["severity"] == 4      # 最新一条的字段
    assert row["game_time"] == "16:26:00 day:0 week:1"
    assert json.loads(row["detail"]) == {"phase": "still"}
    assert row["ts"] == t0                                          # 首次发生保留


def test_merge_resets_delivered_at(tmp_store):
    t0 = time.time()
    record([to_npc(merge_key="k")], tmp_store, now=t0)
    rows = events.select_push("cang", store_dir=tmp_store, now=t0 + 1)
    events.mark_delivered(rows[0]["deliver"], store_dir=tmp_store, now=t0 + 2)
    assert events.list_events("to_npc", store_dir=tmp_store)[0]["delivered_at"] is not None

    record([to_npc(merge_key="k")], tmp_store, now=t0 + 10)          # 合并 → 复位
    row = events.list_events("to_npc", store_dir=tmp_store)[0]
    assert row["delivered_at"] is None
    assert row["count"] == 2


def test_same_key_different_type_or_npc_not_merged(tmp_store):
    t0 = time.time()
    record([to_npc(merge_key="k"), to_npc(type_="chat_started", merge_key="k"),
            to_npc(npc_id="bai", merge_key="k")], tmp_store, now=t0)
    assert len(events.list_events("to_npc", store_dir=tmp_store)) == 3


def test_missing_merge_key_uses_empty_string(tmp_store):
    t0 = time.time()
    record([to_npc(), to_npc()], tmp_store, now=t0)   # 都缺 merge_key → 空串 → 互相合并
    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert len(rows) == 1 and rows[0]["count"] == 2


def test_player_acts_merge_witness_union_and_public_upgrade(tmp_store):
    t0 = time.time()
    record([by_player(merge_key="cook:1:9", witnesses=["cang"])], tmp_store, now=t0)
    record([by_player(merge_key="cook:1:9", witnesses=["bai"],
                      visibility="public")], tmp_store, now=t0 + 10)
    rows = events.list_events("by_player", store_dir=tmp_store)
    assert len(rows) == 1 and rows[0]["count"] == 2
    assert rows[0]["visibility"] == "public"          # 不得把公开事件降级
    for npc in ("cang", "bai"):                       # 目击者并集：都能查到
        got, total = events.visible_acts(npc, store_dir=tmp_store, now=t0 + 20)
        assert total == 1 and got[0]["count"] == 2


def test_player_acts_public_not_downgraded_on_merge(tmp_store):
    t0 = time.time()
    record([by_player(merge_key="k", visibility="public")], tmp_store, now=t0)
    record([by_player(merge_key="k", visibility="witnessed")], tmp_store, now=t0 + 10)
    assert events.list_events("by_player", store_dir=tmp_store)[0]["visibility"] == "public"


def test_player_acts_location_is_merge_boundary(tmp_store):
    t0 = time.time()
    record([by_player(merge_key="k", location="厨房"),
            by_player(merge_key="k", location="客厅")], tmp_store, now=t0)
    rows = events.list_events("by_player", store_dir=tmp_store)
    assert len(rows) == 2                             # location 不同不误并

    record([by_player(merge_key="k", location=None)], tmp_store, now=t0)   # 无地点独立
    assert len(events.list_events("by_player", store_dir=tmp_store)) == 3


def test_batch_same_key_merges_inside_one_transaction(tmp_store):
    t0 = time.time()
    accepted, merged = record([to_npc(merge_key="k"), to_npc(merge_key="k")],
                              tmp_store, now=t0)
    assert (accepted, merged) == (2, 1)
    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert len(rows) == 1 and rows[0]["count"] == 2


def test_record_events_atomic_rollback(tmp_store, monkeypatch):
    """半路失败整批回滚，不暴露半完成状态。"""
    original = events._upsert_interaction
    state = {"n": 0}

    def flaky(conn, ev, moment):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("boom")
        return original(conn, ev, moment)

    monkeypatch.setattr(events, "_upsert_interaction", flaky)
    with pytest.raises(RuntimeError):
        record([to_npc(), to_npc(type_="chat_started")], tmp_store)
    assert events.list_events("to_npc", store_dir=tmp_store) == []


# ── 清理 ────────────────────────────────────────────────

def test_cleanup_deletes_expired_and_cascades_witnesses(tmp_store, monkeypatch):
    monkeypatch.setenv("NPC_EVENTS_KEEP_DAYS", "3650")     # 播种期间不被顺手清理删掉
    now = time.time()
    record([to_npc(merge_key="old")], tmp_store, now=now - 31 * 86400)
    record([to_npc(merge_key="new")], tmp_store, now=now)
    record([by_player(merge_key="old-act", witnesses=["cang"])], tmp_store,
           now=now - 31 * 86400)
    record([by_player(merge_key="new-act", witnesses=["cang"])], tmp_store, now=now)
    assert len(events.list_events("to_npc", store_dir=tmp_store)) == 2   # 播种完好

    monkeypatch.setenv("NPC_EVENTS_KEEP_DAYS", "30")
    deleted = events.cleanup(tmp_store, now=now)

    assert deleted["npc_interactions_expired"] == 1
    assert deleted["player_acts_expired"] == 1
    assert [r["merge_key"] for r in events.list_events("to_npc", store_dir=tmp_store)] == ["new"]
    acts = events.list_events("by_player", store_dir=tmp_store)
    assert [r["merge_key"] for r in acts] == ["new-act"]
    # 级联删除：过期行为的目击者记录也删掉，新行为的保留
    conn = sqlite3.connect(str(events.db_path(tmp_store)))
    try:
        witnesses = conn.execute("SELECT act_id, npc_id FROM act_witnesses").fetchall()
    finally:
        conn.close()
    assert len(witnesses) == 1 and witnesses[0][1] == "cang"


def test_cleanup_cap_deletes_oldest_low_severity_first(tmp_store, monkeypatch):
    monkeypatch.setenv("NPC_EVENTS_MAX", "5000")           # 播种期间不受上限影响
    base = time.time() - 100
    for i in range(4):                                     # 4 条低严重度，越写越新
        record([to_npc(merge_key=f"low{i}", severity=3)], tmp_store, now=base + i)
    record([to_npc(merge_key="high", severity=9)], tmp_store, now=base + 10)
    assert len(events.list_events("to_npc", store_dir=tmp_store)) == 5

    monkeypatch.setenv("NPC_EVENTS_MAX", "3")
    deleted = events.cleanup(tmp_store, now=base + 20)

    assert deleted["npc_interactions_overcap"] == 2   # 超出 3 → 删最旧的 2 条
    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert len(rows) == 3
    severities = sorted(r["severity"] for r in rows)
    assert severities == [3, 3, 9]                    # 高严重度豁免；最旧两条低严重度被删
    assert sorted(r["merge_key"] for r in rows) == ["high", "low2", "low3"]


def test_cleanup_throttle_isolated_per_store_dir(tmp_path):
    """限流状态按解析后的数据库绝对路径隔离：不同 store_dir 互不影响。"""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    old = time.time() - 40 * 86400

    # 第一笔写入顺手清掉过期记录（maybe_cleanup 第一次执行）
    record([to_npc(merge_key="old")], dir_a, now=old)
    assert len(events.list_events("to_npc", store_dir=dir_a)) == 0

    # 限流窗（每库每分钟一次）内：新写入不再触发清理 → 过期记录暂时保留
    record([to_npc(merge_key="old2")], dir_a, now=old)
    assert len(events.list_events("to_npc", store_dir=dir_a)) == 1

    # 不同 store_dir 的限流互不影响：b 库第一次写入照常清理
    record([to_npc(merge_key="old")], dir_b, now=old)
    assert len(events.list_events("to_npc", store_dir=dir_b)) == 0


def test_record_triggers_throttled_cleanup(tmp_store):
    now = time.time()
    record([to_npc(merge_key="old")], tmp_store, now=now - 40 * 86400)
    # 第一笔新写入顺手清掉过期记录（old 已被 30 天保留期清理删掉）
    record([to_npc(merge_key="new")], tmp_store, now=now)
    rows = events.list_events("to_npc", store_dir=tmp_store)
    assert [r["merge_key"] for r in rows] == ["new"]


# ── 推送选取 ────────────────────────────────────────────

def test_select_push_new_events_sorted_by_severity(tmp_store):
    now = time.time()
    record([
        to_npc(type_="low_started", severity=2, merge_key="low"),
        to_npc(type_="high_started", severity=8, merge_key="high"),
        to_npc(type_="mid_started", severity=5, merge_key="mid"),
    ], tmp_store, now=now)
    rows = events.select_push("cang", store_dir=tmp_store, now=now + 1)
    assert len(rows) == 3
    assert all(r["new"] for r in rows)
    assert [r["severity"] for r in rows] == [8, 5, 2]  # severity 降序
    assert rows[0]["text"].startswith("[新] ")
    assert all(len(r["deliver"]) == 1 for r in rows)   # 每条新事带 (id, last_ts)


def test_select_push_overflow_collapses_into_one_line(tmp_store, monkeypatch):
    monkeypatch.setenv("NPC_EVENT_PUSH_MAX", "2")
    now = time.time()
    record([
        to_npc(type_="a_started", severity=7, merge_key="a", summary="玩家对你发起争执",
               label="争执"),
        to_npc(type_="b_started", severity=7, merge_key="b", summary="玩家对你恶意调侃",
               label="调侃"),
        to_npc(type_="c_started", severity=4, merge_key="c", summary="玩家对你寒暄",
               label="寒暄"),
        to_npc(type_="d_started", severity=4, merge_key="d", summary="玩家对你寒暄",
               label="寒暄"),
        to_npc(type_="e_started", severity=3, merge_key="e", summary="玩家对你挥手",
               label="挥手"),
    ], tmp_store, now=now)
    rows = events.select_push("cang", store_dir=tmp_store, now=now + 1)
    assert len(rows) == 3                             # 2 条逐条 + 1 条折叠
    head = [r for r in rows if r["text"].startswith("[新]")]
    assert len(head) == 2
    collapse = rows[2]
    assert collapse["text"] == "另有 3 条较轻的：寒暄×2、挥手×1"
    # 折叠行保留所含全部事件的 (id, last_ts)（×n 按发生次数计）
    assert len(collapse["deliver"]) == 3 and collapse["new"] is True


def test_select_push_recent_window_and_prefix(tmp_store):
    now = time.time()
    record([to_npc(merge_key="fresh")], tmp_store, now=now - 60)        # 送达后 30 分钟内
    record([to_npc(merge_key="stale")], tmp_store, now=now - 45 * 60)   # 送达后出窗
    mark = []
    for r in events.select_push("cang", store_dir=tmp_store, now=now):
        mark.extend(r["deliver"])
    events.mark_delivered(mark, store_dir=tmp_store, now=now)

    rows = events.select_push("cang", store_dir=tmp_store, now=now)
    assert len(rows) == 1                             # 只剩窗口内的近事
    assert rows[0]["text"].startswith("[之前] ")
    assert rows[0]["new"] is False and rows[0]["deliver"] == []   # 近事不改已读状态


def test_select_push_recent_limit(tmp_store, monkeypatch):
    monkeypatch.setenv("NPC_EVENT_RECENT_MAX", "2")
    now = time.time()
    for i in range(4):                                # 4 条已送达的近事
        record([to_npc(merge_key=f"r{i}")], tmp_store, now=now - 60 - i)
        mark = []
        for r in events.select_push("cang", store_dir=tmp_store, now=now):
            mark.extend(r["deliver"])
        events.mark_delivered(mark, store_dir=tmp_store, now=now)
    rows = events.select_push("cang", store_dir=tmp_store, now=now)
    assert len(rows) == 2                             # 近事条数上限


def test_select_push_line_format(tmp_store):
    now = time.time()
    record([to_npc(merge_key="k")], tmp_store, now=now - 185)
    record([to_npc(merge_key="k", game_time="16:25:11.040 day:0 week:1")],
           tmp_store, now=now - 180)                  # 合并 → count=2，字段取最新
    record([to_npc(type_="plain_started", merge_key="p", summary="玩家对你挥手示意")],
           tmp_store, now=now)
    rows = events.select_push("cang", store_dir=tmp_store, now=now)
    hug = [r for r in rows if "拥抱" in r["text"]][0]
    assert hug["text"] == ("[新] 3分钟前（16:25:11.040 day:0 week:1）"
                           "玩家对你发起了拥抱互动 ×2")
    plain = [r for r in rows if "挥手" in r["text"]][0]
    # 无 game_time → 省略括号；count=1 → 不出现 ×n
    assert "（" not in plain["text"] and "×" not in plain["text"]
    assert plain["text"].startswith("[新] ")


def test_select_push_empty_when_no_events(tmp_store):
    assert events.select_push("cang", store_dir=tmp_store) == []


# ── 版本化已读标记 ─────────────────────────────────────

def test_mark_delivered_version_guard(tmp_store):
    """回合进行中同一事件又发生合并 → last_ts 已变，旧回合不得误标。"""
    t0 = time.time()
    record([to_npc(merge_key="k")], tmp_store, now=t0)
    rows = events.select_push("cang", store_dir=tmp_store, now=t0 + 1)
    stale_pairs = rows[0]["deliver"]

    record([to_npc(merge_key="k")], tmp_store, now=t0 + 5)          # 回合中合并
    assert events.mark_delivered(stale_pairs, store_dir=tmp_store, now=t0 + 6) == 0
    assert events.list_events("to_npc", store_dir=tmp_store)[0]["delivered_at"] is None

    fresh = events.select_push("cang", store_dir=tmp_store, now=t0 + 6)
    assert events.mark_delivered(fresh[0]["deliver"], store_dir=tmp_store, now=t0 + 7) == 1
    assert events.list_events("to_npc", store_dir=tmp_store)[0]["delivered_at"] is not None


def test_mark_delivered_only_touches_matching_rows(tmp_store):
    t0 = time.time()
    record([to_npc(npc_id="cang", merge_key="k"), to_npc(npc_id="bai", merge_key="k")],
           tmp_store, now=t0)
    pairs = events.select_push("cang", store_dir=tmp_store, now=t0 + 1)[0]["deliver"]
    assert events.mark_delivered(pairs, store_dir=tmp_store, now=t0 + 2) == 1
    rows = {r["npc_id"]: r for r in events.list_events("to_npc", store_dir=tmp_store)}
    assert rows["cang"]["delivered_at"] is not None
    assert rows["bai"]["delivered_at"] is None


# ── 可见查询（check_player_log 的数据面）────────────────

def test_visible_acts_witness_and_public_visibility(tmp_store):
    now = time.time()
    record([
        by_player(type_="cook_started", merge_key="c", witnesses=["cang"]),
        by_player(type_="repair_started", merge_key="r", visibility="public"),
        by_player(type_="secret_started", merge_key="s", witnesses=["bai"]),
    ], tmp_store, now=now)

    cang_rows, cang_total = events.visible_acts("cang", store_dir=tmp_store, now=now + 1)
    types = {r["type"] for r in cang_rows}
    assert types == {"cook_started", "repair_started"}   # 目击的 + 公开的
    assert cang_total == 2
    assert "secret_started" not in types                 # 非目击且非公开 → 查不到

    bai_rows, _ = events.visible_acts("bai", store_dir=tmp_store, now=now + 1)
    assert {r["type"] for r in bai_rows} == {"repair_started", "secret_started"}


def test_visible_acts_keyword_minutes_limit(tmp_store):
    now = time.time()
    record([
        by_player(type_="cook_started", merge_key="c", summary="玩家开始做饭",
                  label="开始做饭", location="厨房", witnesses=["cang"]),
        by_player(type_="repair_started", merge_key="r", summary="玩家开始修理水龙头",
                  label="修理", location="厨房", witnesses=["cang"]),
    ], tmp_store, now=now - 120)
    record([by_player(type_="watch_started", merge_key="w", summary="玩家开始看电视",
                      label="看电视", witnesses=["cang"])], tmp_store, now=now - 10)

    # keyword 子串匹配 type/label/summary
    rows, total = events.visible_acts("cang", keyword="做饭", store_dir=tmp_store, now=now)
    assert total == 1 and rows[0]["type"] == "cook_started"
    rows, total = events.visible_acts("cang", keyword="repair", store_dir=tmp_store, now=now)
    assert total == 1
    rows, total = events.visible_acts("cang", keyword="修理", store_dir=tmp_store, now=now)
    assert total == 1

    # minutes 时间窗：只剩 10 秒前的看电视（做饭/修理都在 2 分钟前）
    rows, total = events.visible_acts("cang", minutes=1, store_dir=tmp_store, now=now)
    assert total == 1 and rows[0]["type"] == "watch_started"

    # limit：只取最新 1 条，但总数照报（剩余行数由调用方算）
    rows, total = events.visible_acts("cang", limit=1, store_dir=tmp_store, now=now)
    assert len(rows) == 1 and total == 3


def test_visible_acts_keyword_escapes_like_wildcards(tmp_store):
    now = time.time()
    record([by_player(type_="a_b", merge_key="k1", summary="玩家做了%奇怪%事",
                      witnesses=["cang"]),
            by_player(type_="axb", merge_key="k2", summary="别的", witnesses=["cang"])],
           tmp_store, now=now)
    rows, total = events.visible_acts("cang", keyword="a_b", store_dir=tmp_store, now=now)
    assert total == 1 and rows[0]["type"] == "a_b"       # _ 不当通配符
    rows, total = events.visible_acts("cang", keyword="%奇怪%", store_dir=tmp_store, now=now)
    assert total == 1                                    # % 不当通配符


def test_format_log_lines(tmp_store):
    now = time.time()
    record([by_player(type_="cook_started", merge_key="c", label="开始做饭",
                      summary="玩家开始做饭", location="厨房", witnesses=["cang"])],
           tmp_store, now=now - 120)
    record([by_player(type_="cook_started", merge_key="c", label="开始做饭",
                      summary="玩家开始做饭", location="厨房", witnesses=["cang"])],
           tmp_store, now=now - 60)                      # 合并 → count=2
    record([by_player(type_="repair_started", merge_key="r", summary="玩家开始修理",
                      witnesses=["cang"])], tmp_store, now=now - 90)
    record([by_player(type_="watch_started", merge_key="w", summary="玩家开始看电视",
                      witnesses=["cang"])], tmp_store, now=now - 10)

    rows, total = events.visible_acts("cang", limit=2, store_dir=tmp_store, now=now)
    assert total == 3                                   # cook(合并行) + repair + watch
    text = events.format_log_lines(rows, remaining=total - len(rows), now=now)
    lines = text.split("\n")
    assert len(lines) == 3
    cook = [ln for ln in lines if "做饭" in ln][0]
    assert cook == f"[{rows[1]['id'][:6]}] 1分钟前｜开始做饭｜玩家开始做饭 ×2｜厨房"
    watch = [ln for ln in lines if "看电视" in ln][0]
    assert watch == f"[{rows[0]['id'][:6]}] 10秒前｜watch_started｜玩家开始看电视"
    assert lines[-1] == "还有 1 条没列出"

    assert events.format_log_lines([], now=now) == "没有符合条件的记录"


# ── 目录行 ─────────────────────────────────────────────

def test_digest_info_counts_and_top_labels(tmp_store, monkeypatch):
    monkeypatch.setenv("NPC_PLAYER_LOG_DIGEST_MINUTES", "120")
    now = time.time()
    record([
        by_player(type_="cook_started", merge_key="c1", label="开始做饭",
                  witnesses=["cang"]),
        by_player(type_="cook_started", merge_key="c2", label="开始做饭",
                  witnesses=["cang"]),
        by_player(type_="cook_started", merge_key="c3", label="开始做饭",
                  witnesses=["cang"]),
        by_player(type_="repair_started", merge_key="r", label="修理", witnesses=["cang"]),
        by_player(type_="repair_started", merge_key="r2", label="修理", witnesses=["cang"]),
        by_player(type_="watch_started", merge_key="w", label="看电视", witnesses=["cang"]),
    ], tmp_store, now=now)
    # 再合并两次 c1（count 增而不增行数）
    record([by_player(type_="cook_started", merge_key="c1", label="开始做饭")],
           tmp_store, now=now + 5)
    record([by_player(type_="cook_started", merge_key="c1", label="开始做饭")],
           tmp_store, now=now + 10)

    line, has = events.digest_info("cang", store_dir=tmp_store, now=now + 20)
    assert has is True
    assert line.startswith("你能查到最近 2 小时内 6 条玩家行为（")   # 总条数按行数计
    assert "开始做饭×5" in line       # count 口径：c1 合并到 count=3，加 c2/c3 各 1
    assert "修理×2" in line and "看电视×1" in line
    # 前 3 个 label 按次数降序
    assert line.index("开始做饭×5") < line.index("修理×2") < line.index("看电视×1")

    assert events.has_visible("cang", store_dir=tmp_store, now=now + 20) is True
    assert events.digest_line("cang", store_dir=tmp_store, now=now + 20) == line


def test_digest_info_excludes_out_of_window_and_invisible(tmp_store):
    now = time.time()
    record([by_player(type_="old_started", merge_key="o", witnesses=["cang"])],
           tmp_store, now=now - 3 * 3600)              # 3 小时前 → 出窗
    record([by_player(type_="secret_started", merge_key="s", witnesses=["bai"])],
           tmp_store, now=now)                          # 别人的目击 → cang 不可见
    line, has = events.digest_info("cang", store_dir=tmp_store, now=now)
    assert (line, has) == ("", False)                   # 没有记录整块省略


# ── 校验（每个非法字段都点名）──────────────────────────

def _ev(patch: dict, drop: list) -> dict:
    ev = {"kind": "to_npc", "npc_id": "cang", "type": "t", "summary": "s"}
    ev.update(patch)
    for key in drop:
        ev.pop(key, None)
    return ev


@pytest.mark.parametrize("patch, drop, expect", [
    ({"kind": "to_other"}, [], "kind"),
    ({}, ["npc_id"], "npc_id"),                          # 缺 npc_id
    ({"npc_id": "../x"}, [], "npc_id"),                  # 穿越企图
    ({}, ["type"], "type"),                              # 缺 type
    ({"type": "坏 类型!"}, [], "type"),
    ({"type": "x" * 41}, [], "type"),
    ({}, ["summary"], "summary"),                        # 缺 summary
    ({"summary": ""}, [], "summary"),
    ({"summary": "s" * 201}, [], "summary"),
    ({"severity": 10}, [], "severity"),
    ({"severity": True}, [], "severity"),
    ({"severity": "3"}, [], "severity"),
    ({"label": "超过八个字符的标签"}, [], "label"),
    ({"visibility": "secret"}, [], "visibility"),
    ({"game_time": 123}, [], "game_time"),
    ({"merge_key": ["x"]}, [], "merge_key"),
    ({"location": 4.5}, [], "location"),
    ({"kind": "by_player", "witnesses": "cang"}, ["npc_id"], "witnesses"),
    ({"kind": "by_player", "witnesses": ["../x"]}, ["npc_id"], "witnesses[0]"),
    ({"kind": "by_player", "detail": [1, 2]}, ["npc_id"], "detail"),
    ({"kind": "by_player", "detail": {"sim": object()}}, ["npc_id"], "detail"),
    ({"kind": "by_player", "detail": {"blob": "x" * 5000}}, ["npc_id"], "detail"),
])
def test_validate_rejects_each_bad_field(patch, drop, expect):
    with pytest.raises(ValueError) as err:
        events.validate_events([_ev(patch, drop)])
    assert f"events[0].{expect}" in str(err.value)


def test_validate_rejects_bad_batch_shape():
    for bad in (None, "x", {}, to_npc()):
        with pytest.raises(ValueError):
            events.validate_events(bad)
    with pytest.raises(ValueError):
        events.validate_events([])                      # 空批次
    with pytest.raises(ValueError):
        events.validate_events([to_npc()] * 51)         # 超过 50 条


def test_validate_ok_normalizes_defaults():
    got = events.validate_events([{
        "kind": "to_npc", "npc_id": "cang", "type": "hug_started",
        "summary": "玩家对你发起了拥抱互动"}])
    assert got[0]["severity"] == 3 and got[0]["label"] is None
    assert got[0]["merge_key"] is None and got[0]["detail_json"] is None

    got = events.validate_events([{"kind": "by_player", "type": "cook",
                                   "summary": "玩家开始做饭",
                                   "witnesses": ["cang", "cang", "bai"]}])
    assert got[0]["witnesses"] == ["cang", "bai"]       # 去重（保序）
    assert got[0]["visibility"] == "witnessed"          # 缺省


def test_validate_blank_optional_fields_treated_as_missing():
    got = events.validate_events([{"kind": "to_npc", "npc_id": "cang", "type": "t",
                                   "summary": "s", "game_time": "  ",
                                   "merge_key": "", "location": "\t",
                                   "label": ""}])
    assert got[0]["game_time"] is None and got[0]["merge_key"] is None
    assert got[0]["location"] is None and got[0]["label"] is None


def test_validate_rejects_second_event_keeps_batch_atomic_message():
    events_list = [to_npc(), {"kind": "to_npc", "npc_id": "cang", "type": "t"}]  # 缺 summary
    with pytest.raises(ValueError) as err:
        events.validate_events(events_list)
    assert "events[1].summary" in str(err.value)


# ── 调试查询 ───────────────────────────────────────────

def test_list_events_filters_and_order(tmp_store):
    now = time.time()
    record([
        to_npc(npc_id="cang", merge_key="a"),
        to_npc(npc_id="cang", merge_key="b"),
        to_npc(npc_id="bai", merge_key="c"),
        by_player(merge_key="p1", witnesses=["cang"]),
        by_player(merge_key="p2", witnesses=["bai"]),
    ], tmp_store, now=now)
    record([to_npc(npc_id="cang", merge_key="a")], tmp_store, now=now + 5)  # a 行更新

    rows = events.list_events("to_npc", npc_id="cang", store_dir=tmp_store)
    assert [r["npc_id"] for r in rows] == ["cang", "cang"]
    assert rows[0]["last_ts"] >= rows[1]["last_ts"]     # last_ts 倒序
    assert all("delivered_at" in r for r in rows)       # 原始字段保留

    acts = events.list_events("by_player", npc_id="cang", store_dir=tmp_store)
    assert [r["merge_key"] for r in acts] == ["p1"]     # 只见自己可见的行为

    with pytest.raises(ValueError):
        events.list_events("both", store_dir=tmp_store)
