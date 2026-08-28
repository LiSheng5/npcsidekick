"""任务书#04 记忆管家测试 — 触发判定 / 归档压缩 / 红线保护 / 报告 / 流民跳过。"""
import json

import pytest

from npc import housekeeper as hk
from npc.events_archive import parse_log_line
from npc.npc import NPC
from npc.world import (LOG_TIER_AUTONOMOUS, SUMMARY_PREFIX,
                       compress_archive_log, log_tier)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NPC_HOUSEKEEPER", raising=False)


def _npc(tmp_path, use_llm=False, ephemeral=False):
    return NPC(persona={"id": "h1", "name": "管家测"}, store_dir=str(tmp_path),
               use_llm=use_llm, ephemeral=ephemeral)


class _TidyLLM:
    """回预设三分类 JSON, 记录 prompt(断言 SCHED 路径被走)。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return type("R", (), {"content": self.reply})()


_TIDY_REPLY = ('[{"mtype": "persona", "content": "玩家信任我", "importance": 8},'
               ' {"mtype": "instruction", "content": "主角要我优先修哨塔", "importance": 7}]')


# ── 触发判定(纯函数) ─────────────────────────────────────────

def test_should_minor_idle_threshold():
    assert hk.should_minor(hk.IDLE_MINOR_TICKS - 1) is False
    assert hk.should_minor(hk.IDLE_MINOR_TICKS) is True
    # 冷却未过 → 不触发
    assert hk.should_minor(hk.IDLE_MINOR_TICKS, cooldown_ticks=1) is False
    assert hk.should_minor(hk.IDLE_MINOR_TICKS, cooldown_ticks=0) is True


def test_memory_tokens_and_emergency(tmp_path):
    m = _npc(tmp_path).memory
    m.load([{"id": "a", "content": "日" * 2020, "importance": 5,
             "category": "general", "created_at": 0.0}] * 3)
    assert hk.memory_tokens(m) == 6060
    assert hk.should_emergency(m, max_tokens=6000) is True
    assert hk.should_emergency(m, max_tokens=7000) is False


def test_memory_tokens_excludes_archived(tmp_path):
    """archived 已退出检索上下文 → 不再撑大快满估算(整理后阈值真正可降)。"""
    m = _npc(tmp_path).memory
    m.load([{"id": "a", "content": "日" * 2000, "importance": 5,
             "category": "general", "created_at": 0.0},
            {"id": "b", "content": "日" * 4000, "importance": 5,
             "category": "archived", "created_at": 0.0}])
    assert hk.memory_tokens(m) == 2000


# ── 触发器状态机(简洁性 review: TriggerState 收编 tick 循环内联状态) ──

def test_trigger_minor_accumulates_and_cools():
    gate = hk.TriggerState()
    for _ in range(hk.IDLE_MINOR_TICKS - 1):
        assert gate.on_tick(busy=False) is None
    assert gate.on_tick(busy=False) == "minor"      # 静置满 → 触发
    assert gate.on_tick(busy=False) is None         # 冷却期内不触发


def test_trigger_busy_resets_idle():
    gate = hk.TriggerState()
    for _ in range(hk.IDLE_MINOR_TICKS - 1):
        gate.on_tick(busy=False)
    gate.on_tick(busy=True)                        # 忙 → 累积清零
    for _ in range(hk.IDLE_MINOR_TICKS - 1):
        assert gate.on_tick(busy=False) is None
    assert gate.on_tick(busy=False) == "minor"     # 重新攒满才触发


def test_trigger_emergency_cadence_and_cool():
    gate = hk.TriggerState()
    for _ in range(hk.EMERGENCY_EVERY_TICKS - 1):
        assert gate.on_tick(busy=True) is None      # busy 抑制 minor
    assert gate.on_tick(busy=True) == "emergency"   # 节拍到 → 触发
    for _ in range(hk.EMERGENCY_EVERY_TICKS - 1):
        assert gate.on_tick(busy=True) is None      # 冷却(120 tick) > 节拍(60) → 不触发


# ── 归档压缩(🌗 段压一行; 🌟 逐行保留; 游标契约) ─────────────

def _write_archive(tmp_path, lines):
    f = tmp_path / "arch" / "log_archive.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(json.dumps({"i": i, "text": t}, ensure_ascii=False)
                           for i, t in enumerate(lines)) + "\n",
                 encoding="utf-8")
    return str(f.parent)


def _read_archive(tmp_path):
    f = tmp_path / "arch" / "log_archive.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]


def test_compress_collapses_autonomous_segments(tmp_path):
    lines = ["cang 前往 森林", "cang 前往 河边", "cang 休息",      # 连续自主×3 → 压
             "cang 说: 你好",                                       # 互动 → 保留
             "cang 采集了 1 个木材", "cang 采集了 1 个木材", "cang 采集了 1 个石头",  # ×3 → 压
             "cang 前往 矿洞"]                                      # 孤段 1 条 → 保留
    d = _write_archive(tmp_path, lines)
    reduced = compress_archive_log(d)
    assert reduced == 5                       # 8 行 → 3 行: 自主段 0-2 与 4-7(连续) 各压一行
    recs = _read_archive(tmp_path)
    assert [r["i"] for r in recs] == [0, 3, 4]
    assert recs[0]["text"].startswith(SUMMARY_PREFIX)      # 段首绝对索引保留
    assert "前往森林" in recs[0]["text"] and "前往河边" in recs[0]["text"]
    assert recs[1]["text"] == "cang 说: 你好"              # 🌟 逐行入档不变
    assert recs[2]["text"].startswith(SUMMARY_PREFIX)      # 采集段+孤行(连续) 合压
    assert "采集木材×2" in recs[2]["text"] and "前往矿洞" in recs[2]["text"]


def test_compress_idempotent_and_keeps_garbage(tmp_path):
    lines = ["cang 前往 森林", "cang 前往 河边", "cang 前往 矿洞", "坏行{", "cang 休息",
             "cang 休息", "cang 休息", "cang 休息"]
    d = _write_archive(tmp_path, lines)
    assert compress_archive_log(d) == 5        # 两段各压(3 条→1; 4 条→1)
    again = compress_archive_log(d)            # 幂等: 已压缩行不再分组
    assert again == 0


def test_summary_line_not_parsed_as_event(tmp_path):
    """压缩摘要行不入任何事件正则(风险表第一行: events 回归)。"""
    lines = ["cang 前往 森林", "cang 前往 河边", "cang 前往 矿洞"]
    d = _write_archive(tmp_path, lines)
    compress_archive_log(d)
    recs = _read_archive(tmp_path)
    assert parse_log_line(recs[0]["text"]) is None


def test_cursor_semantics_survive_compression(tmp_path, monkeypatch):
    """压缩后 _log_offset 逻辑条数不变 → 游标/回放不炸(旧客户端零感知)。"""
    lines = ["cang 前往 森林", "cang 前往 河边", "cang 前往 矿洞", "cang 说: 好"]
    d = _write_archive(tmp_path, lines)
    compress_archive_log(d)
    monkeypatch.setenv("NPC_LOG_ARCHIVE_DIR", d)
    monkeypatch.setenv("NPC_LOG_TAIL", "10")
    world = {"log": ["cang 说: 尾行"], "_log_offset": 4}   # 4 条已迁走(逻辑条数不因压缩改变)
    from npc.events_archive import events_since
    evs, total = events_since(type("W", (), {"world": world})(), since=0)
    assert total == 5                             # offset(4) + 尾部(1) 不变
    assert [e["type"] for e in evs if e] == ["say", "say"]  # 🌟 互动行回放 + 尾部; 🌗 摘要零事件


# ── 归纳分层(红线 / 降级 / 合并 / 补型 / 报告) ───────────────

def _setup_tidy(tmp_path, llm=None):
    npc = _npc(tmp_path, use_llm=llm is not None)
    if llm is not None:
        npc._llm = llm
    npc.memory.add("去了森林", importance=5)
    npc.memory.add("采了三根木头", importance=5)
    npc.memory.add("路过河边歇了会", importance=5)
    npc.memory.add("主角要我优先修哨塔", importance=5)   # 与 LLM 输出精确同文 → 补 mtype
    # 红线三件套: 高重要度 / reflection / pinned —— 只读不动
    npc.memory.add("玩家救我性命", importance=9)
    npc.memory.add("旧结论不动", importance=8, category="reflection")
    pinned = npc.memory.add("人工作品不动", importance=5)
    for e in npc.memory.all():
        if e["id"] == pinned:
            e["pinned"] = True
    return npc


def test_tidy_redline_untouched_and_demote_merge(tmp_path):
    llm = _TidyLLM(_TIDY_REPLY)
    npc = _setup_tidy(tmp_path, llm=llm)
    actions = hk.tidy_memory(npc, "dawn")
    ops = {a["op"] for a in actions}
    assert {"demote", "merge"} <= ops
    entries = {e["content"]: e for e in npc.memory.all()}
    # 红线三件套未动
    assert entries["玩家救我性命"]["category"] == "general"
    assert entries["旧结论不动"]["category"] == "reflection"
    assert entries["人工作品不动"].get("pinned") is True
    assert entries["人工作品不动"]["category"] == "general"
    # 流水账降级 archived, 不物理删除
    assert entries["去了森林"]["category"] == "archived"
    # 归纳产物落 reflection
    refl = {e["content"] for e in npc.memory.all() if e["category"] == "reflection"}
    assert "玩家信任我" in refl
    # 精确同文 → 补 mtype(passed through 重判定)
    assert entries["主角要我优先修哨塔"].get("mtype") == "instruction"
    assert llm.calls                                # LLM 走了 SCHED 通道


def test_tidy_redline_archived_not_in_retrieve(tmp_path):
    llm = _TidyLLM(_TIDY_REPLY)
    npc = _setup_tidy(tmp_path, llm=llm)
    hk.tidy_memory(npc, "dawn")
    got = [e["content"] for e in npc.memory.retrieve("森林 河边", top_k=10)]
    assert "去了森林" not in got                   # archived 退出检索上下文
    assert "玩家救我性命" in got                   # 红线高重要度仍在
    assert "去了森林" in [e["content"] for e in npc.memory.all()]  # 证据链仍在卡上


def test_tidy_no_llm_silent(tmp_path):
    npc = _setup_tidy(tmp_path, llm=None)          # use_llm=False
    before = [dict(e) for e in npc.memory.all()]
    assert hk.tidy_memory(npc, "dawn") == []
    assert npc.memory.all() == before              # 记忆卡纹丝不动


def test_tidy_unparsable_silent(tmp_path):
    npc = _setup_tidy(tmp_path, llm=_TidyLLM("完全不是 JSON"))
    assert hk.tidy_memory(npc, "dawn") == []
    assert not any(e.get("category") == "archived" for e in npc.memory.all())


def test_report_appends_per_run(tmp_path):
    llm = _TidyLLM(_TIDY_REPLY)
    npc = _setup_tidy(tmp_path, llm=llm)
    hk.tidy_memory(npc, "dawn")
    hk.append_report(npc, "full", [{"op": "probe", "id": "x", "why": "冒烟"}])
    lines = hk.report_path(npc).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[-1])
    assert rec["trigger"] == "full" and rec["actions"][0]["op"] == "probe"


def test_ephemeral_skipped_everywhere(tmp_path, monkeypatch):
    """流民(ephemeral)全跳过: dawn 不归纳不画像, 也不产报告。"""
    npc = _npc(tmp_path, use_llm=True, ephemeral=True)
    npc._llm = _TidyLLM(_TIDY_REPLY)
    npc.memory.add("去了森林", importance=5)
    npc.memory.add("采了三根木头", importance=5)
    npc.memory.add("路过河边歇了会", importance=5)
    calls = []
    monkeypatch.setattr(hk, "tidy_memory", lambda n, t: calls.append(n) or [])
    hk.dawn(npc.world, {"h1": npc})
    hk.emergency_batch([npc])
    assert calls == []                            # 流民不跑归纳
    assert not hk.report_path(npc).exists()
