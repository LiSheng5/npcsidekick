#!/usr/bin/env python3
"""记忆卡治理·一次性迁移工具（docs/记忆卡治理_方案稿.md 2026-08-25 · 用户批复默认档）

用法:
  python scripts/migrate_memory_cards.py                 # dry-run: 只出报告不动文件
  python scripts/migrate_memory_cards.py --apply         # 真写（原文件先备份到 ../store_backup_<时间戳>/）
  python scripts/migrate_memory_cards.py --store npc/store --card amanda   # 指定目录/单卡(id片段过滤)

清洗规则（按序执行）:
  R1 精确去重:   同 (content, category) 只留 created_at 最新一条
  R2 流水账清除: category=general 且 importance<=5 且 content 以全角「完成：」开头 → 删
                 （scheduler 自主日常的签名；玩家正事是半角冒号且 imp>=6，零误伤）
  R3 退化反思:   category=reflection 且 content 含「休息」→ 删（批复=默认档，非全弃）

收尾: reflected_upto 重置为新长度；--apply 走 tmp+rename 原子写；二次运行幂等（0 改动）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path

_ROUTINE_RE = re.compile(r"^完成：")     # 全角冒号 = scheduler 日常循环签名
_BAD_REFLECT_KEYWORD = "休息"


def migrate_card(data: dict) -> tuple[dict, dict]:
    """清洗一张卡的 JSON 结构（纯函数，便于测试）。返回 (新数据, 统计)。"""
    mem = list(data.get("memory", []))
    stats = {"before": len(mem), "r1": 0, "r2": 0, "r3": 0}

    # ── R1 精确去重: 同(content,category) 保最新 ──
    best: dict = {}
    for i, e in enumerate(mem):
        key = (e.get("content", ""), e.get("category", ""))
        if key not in best or e.get("created_at", 0) > mem[best[key]].get("created_at", 0):
            best[key] = i
    keep_idx = sorted(best.values())
    stats["r1"] = len(mem) - len(keep_idx)
    mem = [mem[i] for i in keep_idx]

    # ── R2 流水账清除 ──
    kept: list = []
    for e in mem:
        if (e.get("category") == "general"
                and int(e.get("importance", 5)) <= 5
                and _ROUTINE_RE.match(e.get("content", ""))):
            stats["r2"] += 1
            continue
        kept.append(e)
    mem = kept

    # ── R3 退化反思（默认档: 只删提及休息循环的）──
    kept = []
    for e in mem:
        if e.get("category") == "reflection" and _BAD_REFLECT_KEYWORD in e.get("content", ""):
            stats["r3"] += 1
            continue
        kept.append(e)
    mem = kept

    out = dict(data)
    out["memory"] = mem
    out["reflected_upto"] = len(mem)
    stats["after"] = len(mem)
    return out, stats


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆卡治理一次性迁移 (R1去重/R2流水账/R3退化反思)")
    ap.add_argument("--store", default="npc/store", help="卡片目录 (默认 npc/store)")
    ap.add_argument("--card", default="", help="只处理 id 含此片段的卡 (如 amanda)")
    ap.add_argument("--apply", action="store_true", help="真写；缺省=dry-run 只出报告")
    args = ap.parse_args(argv)

    store = Path(args.store)
    cards = sorted(p for p in store.glob("*_memory.json") if args.card in p.name)
    if not cards:
        print(f"[!] {store} 下没有匹配的 *_memory.json")
        return 1

    backup_dir = None
    if args.apply:
        backup_dir = store.parent / f"{store.name}_backup_{time.strftime('%Y%m%d_%H%M%S')}"
        backup_dir.mkdir(parents=True, exist_ok=True)

    total_before = total_after = 0
    for c in cards:
        raw = c.read_text(encoding="utf-8")
        out, st = migrate_card(json.loads(raw))
        total_before += st["before"]
        total_after += st["after"]
        print(f"== {c.name}: {st['before']} -> {st['after']} 条 "
              f"(R1去重 -{st['r1']} | R2流水账 -{st['r2']} | R3退化反思 -{st['r3']}) "
              f"| 文件 {len(raw) / 1024:.0f}KB")
        if args.apply:
            shutil.copy2(c, backup_dir / c.name)
            tmp = c.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(c)

    tail = f"已写入（备份: {backup_dir}）" if args.apply else "dry-run 预览（加 --apply 落盘）"
    print(f"\n合计: {total_before} -> {total_after} 条 | {tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
