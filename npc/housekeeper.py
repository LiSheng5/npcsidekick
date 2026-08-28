"""NPCSidekick — 记忆管家(任务书#04): 一键整理 · 三触发。

一个循环 + 三触发器, 收拢散落的记忆维护动作:
  🌅 黎明(server _tick_loop 的 _is_dawn_boundary) → 全量大整理:
      归档压缩 + 归纳分层(tidy_memory) + 画像更新(§26: LLM 一日一次)
  🍃 空闲(SCHED 队列深度全 0 且静置 N tick) → 小整理:
      滚窗自主日志 → 摘要压缩(零 LLM)
  ⏰ 快满(记忆卡 token 粗估 ≥ NPC_MEMORY_TOKEN_MAX) → 应急:
      归纳分层腾空间

红线(验收③): importance≥8 / category∈{reflection, consolidated} /
pinned:true 的条目只读不动; 人工编辑与证据链永不物理删除 —— 流水账
只降级(general→archived, 退出检索上下文), 归纳产物以新条目落 reflection,
全程记录进 {id}_report.jsonl(查得回滚得回)。

纪律(验收④): 管家内 LLM 走 SCHED.invoke(P_REFLECT); 触发批次由 server
整批 asyncio.to_thread; 流民(ephemeral)全跳过。总开关 NPC_HOUSEKEEPER
默认关(家规选择加入) —— 关时 server 不接任何触发, 行为与旧版零差异。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agent.config_flags import env_flag, env_num
from agent.logging_config import log
from npc.events_archive import archive_dir, log_tail_cfg
from npc.memory import CATEGORY_ARCHIVED
from npc.world import compress_archive_log, rotate_world_log

# ── 参数(环境变量可调) ─────────────────────────────────────
IDLE_MINOR_TICKS = 20        # 空闲触发: 调度深度全 0 且静置 ≥ 该 tick 数(≈60s)
MINOR_COOLDOWN_TICKS = 120   # 小整理冷却: 触发后再等 N tick(防反复压缩)
EMERGENCY_EVERY_TICKS = 60   # 快满检查节拍: 与 consolidate 脱钩(简洁性 review, 可独立调)
EMERGENCY_COOLDOWN_TICKS = 120  # 快满应急冷却: 整理后 N tick 内不再检(防"全红线"类 NPC 空跑)
MEMORY_TOKEN_MAX_DEFAULT = 6000   # 快满阈值: 中文≈1字1token 的粗估上限
TIDY_WINDOW = 20             # 归纳分层每批最多纳入的流水账条数(LLM 上下文窗)
REPORT_SUFFIX = "_report.jsonl"


def enabled() -> bool:
    """总开关 NPC_HOUSEKEEPER(默认关 —— 关时 server 不接任何触发, 零差异)。"""
    return env_flag("NPC_HOUSEKEEPER")


# ── 触发判定(纯函数, 独立可测) ─────────────────────────────

def should_minor(idle_ticks: int, cooldown_ticks: int = 0) -> bool:
    """🍃 空闲触发: 静置 ≥ IDLE_MINOR_TICKS 且冷却已尽。"""
    return idle_ticks >= IDLE_MINOR_TICKS and cooldown_ticks <= 0


class TriggerState:
    """🍃⏰ 管家触发器状态机(任务书#04): server 每 tick 调一次 on_tick。

    小整理: 调度队列全空且静置 ≥ IDLE_MINOR_TICKS, 触发后冷却 MINOR_COOLDOWN_TICKS。
    快满应急: 每 EMERGENCY_EVERY_TICKS 检一次(节拍与 consolidate 脱钩),
    触发后冷却 EMERGENCY_COOLDOWN_TICKS(防全红线 NPC 空跑)。
    """
    def __init__(self) -> None:
        self._idle = 0
        self._minor_cool = 0
        self._emergency_cool = 0
        self._tick = 0

    def on_tick(self, busy: bool) -> Optional[str]:
        """返回本轮要触发的动作: "minor" / "emergency" / None。busy=调度队列非空。"""
        self._tick += 1
        self._idle = 0 if busy else self._idle + 1
        self._minor_cool = max(0, self._minor_cool - 1)
        self._emergency_cool = max(0, self._emergency_cool - 1)
        if should_minor(self._idle, self._minor_cool):
            self._idle = 0
            self._minor_cool = MINOR_COOLDOWN_TICKS
            return "minor"
        if self._tick % EMERGENCY_EVERY_TICKS == 0 and self._emergency_cool == 0:
            self._emergency_cool = EMERGENCY_COOLDOWN_TICKS
            return "emergency"
        return None


def memory_tokens(memory) -> int:
    """⏰ 记忆卡 token 粗估: 中文≈每字 1 token(可见条目内容长度求和)。

    只算 active(archived 已退出检索上下文) — 与 retrieve 同一口径:
    整理后降级部分不再撑大估算, 阈值真正可降。
    """
    return sum(len(str(e.get("content", ""))) for e in memory.active())


def token_limit() -> int:
    """快满阈值(NPC_MEMORY_TOKEN_MAX, 默认 6000)。server 每 tick 读一次再传。"""
    return env_num("NPC_MEMORY_TOKEN_MAX", MEMORY_TOKEN_MAX_DEFAULT)


def should_emergency(memory, max_tokens: int = 0) -> bool:
    """⏰ 快满触发: 粗估 token ≥ 阈值(默认 NPC_MEMORY_TOKEN_MAX)。"""
    limit = max_tokens or token_limit()
    return memory_tokens(memory) >= limit


# ── 归纳分层(流水账 → 三分类结构化, LLM 走 SCHED+P_REFLECT) ──

def _tidy_candidates(memory) -> List[Dict]:
    """归纳候选 = 流水账: general、imp≤6、无 mtype、未 pin(红线外)。"""
    return [e for e in memory.all()
            if e.get("category") == "general"
            and e.get("importance", 5) <= 6
            and "mtype" not in e
            and not e.get("pinned")]


def tidy_memory(npc, trigger: str) -> List[Dict]:
    """一次归纳分层: 流水账 → LLM 三分类提炼 → 降级/合并/补型。

    三分类提炼复用 memory_card._reflect_entries_typed(提示词/SCHED 纪律
    单一来源, 简洁性 review); 产物: 精确同文的候选补 mtype(重判定),
    其余提炼条新增 reflection(category=reflection, mtype 按 LLM);
    全部源候选降级 general→archived(退出检索上下文, 证据链仍在卡上 —
    永不物理删除)。无 LLM / 解析失败 / 候选不足 3 → 静默不动作。
    返回整理报告 actions。
    """
    if getattr(npc, "ephemeral", False):
        return []
    cands = _tidy_candidates(npc.memory)[-TIDY_WINDOW:]
    if len(cands) < 3:
        return []
    llm = npc._get_llm(role="review")      # 反思档模型(纪律: LLM 必须过 SCHED)
    if llm is None:
        return []
    facts = "\n".join(f"- {e['content']}" for e in cands)
    typed = npc._reflect_entries_typed(llm, facts)   # 三分类提炼(含 SCHED 纪律与解析)
    if not typed:
        return []
    actions: List[Dict] = []
    # ① 精确同文的候选 → 补 mtype(三分类自动重判定)
    by_content = {e.get("content"): e for e in cands}
    fresh = []
    for t in typed:
        hit = by_content.get(t["content"])
        if hit is not None:
            hit["mtype"] = t["mtype"]
            actions.append({"op": "mtype", "id": hit.get("id"),
                            "content": hit["content"][:40],
                            "to": t["mtype"], "why": "三分类重判定"})
        else:
            fresh.append(t)
    # ② 提炼条落 reflection(合并), 与既有反思同文则跳过
    if fresh:
        existing = {e.get("content") for e in npc.memory.all()
                    if e.get("category") == "reflection"}
        for t in fresh:
            if t["content"] in existing:
                continue
            npc.memory.add(t["content"], importance=max(5, min(9, t["importance"])),
                           category="reflection", mtype=t["mtype"])
            actions.append({"op": "merge", "content": t["content"][:40],
                            "mtype": t["mtype"], "why": "流水账归纳"})
    # ③ 源流水账降级 archived(候选已限定 general) —— 永不物理删除
    for e in cands:
        e["category"] = CATEGORY_ARCHIVED
        actions.append({"op": "demote", "id": e.get("id"),
                        "content": e["content"][:40],
                        "why": "已归纳/已分型, 退出检索上下文"})
    if actions:
        npc.save()
        append_report(npc, trigger, actions)
    return actions


def report_path(npc) -> Path:
    """整理报告路径: 与记忆卡同目录 {id}_report.jsonl。"""
    return npc.store_path.parent / f"{npc.actor_id}{REPORT_SUFFIX}"


def append_report(npc, trigger: str, actions: List[Dict]) -> None:
    """追加一条整理报告(查得回滚得回)。失败只告警不抛(报告不值得炸主循环)。"""
    rec = {"at": datetime.now().isoformat(), "trigger": trigger,
           "npc": npc.actor_id, "actions": actions}
    try:
        p = report_path(npc)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.info("housekeeper_report", npc=npc.actor_id, actions=len(actions))
    except Exception as exc:
        log.warning("housekeeper_report_failed", npc=npc.actor_id, error=str(exc))


# ── 归档压缩(滚窗 + 盘上压缩; 零 LLM) ──────────────────────

def roll_and_compress(world) -> int:
    """滚窗(留尾部) + 把归档里的连续自主段压成摘要行。返回压缩减少行数。"""
    try:
        rotate_world_log(world, tail=log_tail_cfg(), archive_dir=archive_dir())
    except Exception as exc:
        log.warning("housekeeper_rotate_failed", error=str(exc))
    return compress_archive_log(archive_dir())


# ── 三触发器入口(server 调用; 流民全跳过) ───────────────────

def minor(world) -> int:
    """🍃 空闲小整理: 滚窗自主日志 → 摘要压缩(零 LLM)。"""
    return roll_and_compress(world)


def emergency_batch(npcs: list) -> None:
    """⏰ 快满应急批次: 候选 NPC 各跑一次归纳分层(to_thread 内; LLM 过 SCHED)。
    流民跳过是入口契约(测试钉住)。"""
    for npc in npcs:
        if not getattr(npc, "ephemeral", False):
            tidy_memory(npc, "full")


def persona_batch(npcs: Dict) -> None:
    """画像批次(§26): 非流民 NPC 各跑一次画像更新。画像一日一次的纪律只有
    这一份实现 — server 管家关时黎明直调, dawn(管家开) 尾部调用。"""
    for npc in npcs.values():
        if not getattr(npc, "ephemeral", False):
            npc.update_persona_profile()


def dawn(world, npcs) -> int:
    """🌅 黎明全量大整理: 压缩 + 归纳分层 + 画像(§26: LLM 一日一次)。流民跳过。"""
    reduced = roll_and_compress(world)
    for npc in npcs.values():
        if getattr(npc, "ephemeral", False):
            continue
        tidy_memory(npc, "dawn")
    persona_batch(npcs)
    return reduced
