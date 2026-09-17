"""NPCSidekick — 记忆体检报告（纯规则层 · 零 LLM token）。

为什么有这一层（任务背景）:
  `npc/housekeeper.py` 的 `append_report` 写的是**机器审计流水**
  （逐条 op/id/content/to/why，给回滚用），只有**每个 NPC 自己**的视角，
  而且**写了没出口** —— 全仓只有 housekeeper 自己调用，没有 API/Console 消费，
  用户花了 token 生成的整理结果**看不到**。

  本模块是**给人看的全局记忆体检**: 不调 LLM（用户对 token 极敏感），只做
  **可数的统计**，出一份跨 NPC 的全局视图，并提供 CLI 出口
  （`python -m npc.memory_report --json`）。

设计口径（务必与管家一致，别重写）:
  - token 粗估 / 快满判定 **强制复用** `housekeeper.memory_tokens` 与
    `housekeeper.should_emergency` —— 口径单一来源，避免两套算法漂移。
    这两个函数只算 `memory.active()`（archived 已退出检索上下文），所以
    **体积类指标天然是"活跃口径"**。
  - 结构计数（总/活跃/归档、三分类、未分型、重复、最旧跨度）也统一在
    **active 条目**上算 —— 与管家阈值口径、tidy 的实际工作集保持一致；
    archived 已降级、tidy 不会再动它，不该混进"待整理"的健康度里。
    总条数/归档数单独作为卡片级体量报告。
  - 流民（ephemeral）: **单列跳过**（不进 per_npc 统计、不计入 NPC 数/总条数），
    但在全局层给出 `ephemeral_count` / `ephemeral_ids` 明示"有这些流民被跳过"——
    既不污染常驻记忆体检，也不静默吞掉它们的存在（理由: 流民 despawn 即忘、
    无记忆卡，本就是管家 skip 的契约；但健康报告若完全隐去会让人误以为"全盘点过"）。

字段名已读源码确认（`npc/memory.py` / `npc/memory_card.py`）:
  - `created_at`: float 时间戳（add() 写入，memory_card 去重时刷新）。
  - `mtype`: 仅当非空才写该键（add 的 mtype="" 不写键）→ "无 mtype 键" = 未分型。
  - `content` / `category`（general/archived/reflection/consolidated/legacy）。
  - `count`: 开启 NPC_MEMORY_DEDUP 时，同文合并计数（1 条 entry 代表 ≥2 次记住）——
    重复检测用 `sum(count)` 而非"条目数"，否则合并后的重复会被漏报。
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from typing import Dict, List, Optional

from npc import housekeeper as hk
from npc.memory import MTYPES

# 三分类白名单（与 npc/memory.py 同一来源，避免抄一份漂移）
_MTYPE_SET = set(MTYPES)


def _per_npc(npc, limit: int, now: float) -> Dict:
    """单个 NPC 的记忆体检（纯统计，零 LLM）。活跃口径见模块 docstring。

    返回 dict 含: 总/活跃/归档条数、token 粗估、是否超阈值、三分类分布、
    未分型数及占比、重复组、最陈旧跨度（小时, 无时间戳条目则 None）。
    """
    memory = npc.memory
    all_entries = memory.all()          # 卡片级体量
    active = memory.active()            # 活跃口径（与管家阈值一致）
    total = len(all_entries)
    active_n = len(active)
    archived = total - active_n

    tokens = hk.memory_tokens(memory)            # 复用: 只算 active
    over = hk.should_emergency(memory, max_tokens=limit)  # 复用: active 口径

    # ── 三分类分布 + 未分型（活跃口径）──
    dist = {m: 0 for m in MTYPES}
    untyped = 0
    for e in active:
        m = e.get("mtype")
        if m in _MTYPE_SET:
            dist[m] += 1
        else:
            # 严谨按"无 mtype 键"算未分型（add 空串不写键，旧卡向下兼容）
            if "mtype" not in e:
                untyped += 1
            else:
                # 有键但值非法（理论不该出现）— 视作未分型，避免漏计
                untyped += 1
    untyped_ratio = (untyped / active_n) if active_n else 0.0

    # ── 重复组（活跃口径; count 字段代表合并后的多次记住）──
    groups: Dict[str, List[int]] = defaultdict(list)
    for e in active:
        groups[e.get("content", "")].append(int(e.get("count", 1)))
    dup_groups: List[Dict] = []
    for content, counts in groups.items():
        times = sum(counts)
        if times >= 2:
            dup_groups.append({"content": content, "entries": len(counts),
                               "times": times})
    dup_groups.sort(key=lambda g: (-g["times"], -g["entries"], g["content"]))

    # ── 最陈旧跨度（活跃口径; 跳过无时间戳条目）──
    stamps = [e.get("created_at") for e in active
              if isinstance(e.get("created_at"), (int, float))]
    oldest_span_hours = round((now - min(stamps)) / 3600.0, 4) if stamps else None

    return {
        "actor_id": getattr(npc, "actor_id", ""),
        "total": total,
        "active": active_n,
        "archived": archived,
        "tokens": tokens,
        "over_threshold": bool(over),
        "mtype_distribution": dist,
        "untyped": untyped,
        "untyped_ratio": round(untyped_ratio, 4),
        "duplicate_groups": dup_groups,
        "duplicate_group_count": len(dup_groups),
        "oldest_span_hours": oldest_span_hours,
    }


def survey(npcs: Dict, world: Optional[Dict] = None, *,
           top_n: int = 10, now: Optional[float] = None) -> Dict:
    """记忆体检总入口: 跨 NPC 结构化统计（纯规则 · 零 LLM）。

    参数:
      npcs:   {actor_id: NPC} —— 每个 NPC 需有 `.memory` / `.actor_id` /
              `.ephemeral`（NPC 类天然具备）。
      world:  保留参数（当前未用，留给未来全局上下文扩展；不强制）。
      top_n:  全库重复榜取前 N（默认 10）。
      now:    当前时间戳覆盖（测试可注入以确定最旧跨度）。

    返回结构化 dict，至少含 `per_npc`（每 NPC）与 `global`（全局）。
    可被 `json.dumps` 直接序列化，供 CLI `--json` / CI 消费。
    """
    now = time.time() if now is None else now
    limit = hk.token_limit()            # 复用管家阈值（NPC_MEMORY_TOKEN_MAX, 默认 6000）

    per_npc: Dict[str, Dict] = {}
    ephemeral_ids: List[str] = []

    # 全局重复聚合: content -> {times, npc_count}
    dup_agg: Dict[str, Dict] = defaultdict(lambda: {"times": 0, "npcs": set()})

    for aid, npc in npcs.items():
        if getattr(npc, "ephemeral", False):
            ephemeral_ids.append(getattr(npc, "actor_id", str(aid)))
            continue                    # 流民单列跳过（见模块 docstring）
        s = _per_npc(npc, limit, now)
        per_npc[s["actor_id"] or str(aid)] = s
        for g in s["duplicate_groups"]:
            slot = dup_agg[g["content"]]
            slot["times"] += g["times"]
            slot["npcs"].add(s["actor_id"])

    # ── 全局维度 ──
    npc_count = len(per_npc)
    total_entries = sum(s["total"] for s in per_npc.values())
    total_tokens = sum(s["tokens"] for s in per_npc.values())
    over_threshold_count = sum(1 for s in per_npc.values() if s["over_threshold"])

    # 最该整理的 NPC 榜: 超阈程度(overage) 优先, 其次未分型占比, 再次体积
    ranking = []
    for aid, s in per_npc.items():
        overage = max(0, s["tokens"] - limit)
        ranking.append({
            "actor_id": aid,
            "tokens": s["tokens"],
            "overage": overage,
            "untyped": s["untyped"],
            "untyped_ratio": s["untyped_ratio"],
        })
    ranking.sort(key=lambda r: (-r["overage"], -r["untyped_ratio"],
                                -r["tokens"], r["actor_id"]))

    # 全库重复最多的内容 top N
    top_duplicates = []
    for content, slot in dup_agg.items():
        top_duplicates.append({
            "content": content,
            "times": slot["times"],
            "npc_count": len(slot["npcs"]),
        })
    top_duplicates.sort(key=lambda d: (-d["times"], -d["npc_count"], d["content"]))
    top_duplicates = top_duplicates[:max(0, int(top_n))]

    return {
        "per_npc": per_npc,
        "global": {
            "npc_count": npc_count,
            "ephemeral_count": len(ephemeral_ids),
            "ephemeral_ids": sorted(ephemeral_ids),
            "total_entries": total_entries,
            "total_tokens": total_tokens,
            "threshold_limit": limit,
            "over_threshold_count": over_threshold_count,
            "tidy_ranking": ranking,
            "top_duplicates": top_duplicates,
        },
    }


# ── CLI（出口: 机器/CI 看 --json, 人看默认表）────────────────────

def _quiet_logs() -> None:
    """把 structlog 压到 WARNING —— `--json` 时保证 **stdout 只有 JSON**（CI 要能直接解析）。

    注意 `agent.logging_config._LazyLogger` 会在**首次使用**时自行 configure 成 INFO/console，
    所以除了 configure_logging 还得把它的 `_configured` 置位，否则第一条日志就把级别改回去。
    任何异常都吞掉（最坏结果只是日志噪音，不影响体检结果）。照抄 benchmark.py。
    """
    try:
        from agent.logging_config import _LazyLogger, configure_logging
        configure_logging(level="WARNING", mode="console")
        _LazyLogger._configured = True
    except Exception:
        pass


def _load_npcs_from_store(store_dir: str) -> Dict:
    """从记忆卡目录加载所有 {id}_memory.json → {id: NPC}。读坏的卡静默跳过。"""
    from npc.npc import NPC
    from pathlib import Path
    out: Dict = {}
    base = Path(store_dir)
    if not base.is_dir():
        return out
    for p in sorted(base.glob("*_memory.json")):
        npc_id = p.name[: -len("_memory.json")]
        try:
            npc = NPC.load(npc_id, store_dir=str(base))
            out[npc_id] = npc
        except Exception:
            continue
    return out


def _print_report(result: Dict) -> None:
    g = result["global"]
    print("记忆体检报告（纯规则 · 零 LLM）")
    print(f"  NPC 数(常驻): {g['npc_count']}  流民跳过: {g['ephemeral_count']}  "
          f"总条数: {g['total_entries']}  总体积(token粗估): {g['total_tokens']}")
    print(f"  快满阈值: {g['threshold_limit']}  超阈值 NPC 数: {g['over_threshold_count']}")

    print()
    header = (f"{'NPC':<14}{'总':>5}{'活跃':>6}{'归档':>6}{'token':>8}"
              f"{'超阈':>5}{'pers':>6}{'epis':>6}{'inst':>6}"
              f"{'未分型':>7}{'重复组':>7}{'最旧(h)':>9}")
    print(header)
    print("-" * len(header))
    for aid, s in result["per_npc"].items():
        md = s["mtype_distribution"]
        old = s["oldest_span_hours"]
        old_s = f"{old:.2f}" if old is not None else "-"
        print(f"{aid:<14}{s['total']:>5}{s['active']:>6}{s['archived']:>6}"
              f"{s['tokens']:>8}{'是' if s['over_threshold'] else '否':>5}"
              f"{md.get('persona', 0):>6}{md.get('episodic', 0):>6}"
              f"{md.get('instruction', 0):>6}{s['untyped']:>7}"
              f"{s['duplicate_group_count']:>7}{old_s:>9}")

    if g["tidy_ranking"]:
        print()
        print("最该整理的 NPC 榜（按超阈程度 / 未分型占比）:")
        for rank, item in enumerate(g["tidy_ranking"], 1):
            print(f"  {rank}. {item['actor_id']}  token={item['tokens']} "
                  f"(超 {item['overage']})  未分型 {item['untyped_ratio']:.2%}")

    if g["top_duplicates"]:
        print()
        print(f"全库重复最多的内容 top {len(g['top_duplicates'])}:")
        for d in g["top_duplicates"]:
            snippet = d["content"][:40] if d["content"] else "(空内容)"
            print(f"  ×{d['times']}（{d['npc_count']} 个 NPC）: {snippet}")


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。返回退出码（体检不卡回归，恒 0；仅 `--json` 保证纯 JSON）。"""
    parser = argparse.ArgumentParser(prog="python -m npc.memory_report",
                                     description="NPCSidekick 记忆体检（零 LLM）")
    parser.add_argument("--json", action="store_true",
                        help="输出结构化 JSON（CI 消费；stdout 保证只有 JSON）")
    parser.add_argument("--quiet", action="store_true", help="压掉引擎 INFO 日志")
    parser.add_argument("--store", default="npc/store",
                        help="记忆卡目录（默认 npc/store；会从 *_memory.json 加载）")
    parser.add_argument("--top", type=int, default=10,
                        help="全库重复榜取前 N（默认 10）")
    args = parser.parse_args(argv)

    if args.json or args.quiet:
        _quiet_logs()          # 必须在跑之前：日志由加载期产生

    npcs = _load_npcs_from_store(args.store)
    result = survey(npcs, top_n=args.top)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
