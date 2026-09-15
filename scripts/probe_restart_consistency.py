#!/usr/bin/env python3
"""V-02 探针：多 NPC 重启一致性（只读实测 · 结论见 docs/NPC大脑架构.md §29.4 / §30.1）。

背景（审计 V-02 / 技术债 TD2）：
  内存里多 NPC **共享同一个 world 对象**，但落盘时**每个 NPC 各写一份整 world 快照**
  （`memory_card.py:113 save()` → `_world_for_card()`）。
  重启时 `server.load_village()`（`npc/server.py:283-296`）取
  **cast 迭代顺序里第一个"有记忆卡"的 NPC 的那张卡的 world** 当权威，其余 NPC 的卡里世界被丢弃。
  → 这条语义**隐式、顺序敏感、无测试钉住**。

本探针实测五件事（全部只读；store 用系统临时目录，跑完即删）：
  ① 盘上两张卡的 world 快照是否相同
  ② 正常重启（两张卡都新鲜）→ 状态是否一致
  ③ 把**非首位**的卡连 `saved_at` 一起改旧 → 是否被采纳（不该）
  ④ 反转角色表顺序 → 权威是否换人（T-02 修复前：换 → 整村回退）
  ⑤ 只改内容、伪造一个最新 `saved_at` → **会被采纳**（方案②的已知边界）

用法:
  python scripts/probe_restart_consistency.py

历史结论（2026-09-15）：
  - T-02 修复**前**：权威 = cast 顺序第一张卡 → ④ 反转顺序即整村回退（tick 12→1 / delivered 木材 5→0 / 位置 村庄→矿洞）。
  - T-02 修复**后**（按 `saved_at` 选最新卡）：④ 不再回退；⑤ 手改卡的人要自己负责时间戳。
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 项目根入 path(任意 cwd 可跑)

from npc.persona_loader import load_personas_from_dir
from npc.scheduler import tick_round
from npc.server import load_village

ROOT = Path(__file__).resolve().parents[1]
PERSONAS_DIR = ROOT / "npc" / "personas"   # 用绝对路径: 任意 cwd 都能跑


def fingerprint(w: dict) -> dict:
    """世界指纹：只看会"回退"的字段。"""
    return {
        "tick": w.get("_tick"),
        "delivered": dict(w.get("delivered", {})),
        "actors": {k: v.get("position") for k, v in w.get("actors", {}).items()},
        "inv": {k: dict(v.get("inventory", {})) for k, v in w.get("actors", {}).items()},
        "log_len": len(w.get("log", [])),
    }


def cards(store: Path) -> dict:
    return {f.name: json.loads(f.read_text(encoding="utf-8-sig"))
            for f in sorted(store.glob("*_memory.json"))}


def main() -> None:
    store = Path(tempfile.mkdtemp(prefix="v02_"))
    try:
        personas = load_personas_from_dir(str(PERSONAS_DIR))
        cast = {k: personas[k] for k in sorted(personas)}   # 顺序 = 目录 glob 排序
        print(f"角色表顺序: {list(cast)}\n")

        # ① 全新世界跑 12 帧 + 每张卡都 save（模拟 server._save_on_transitions）
        world, npcs = load_village(store_dir=str(store), personas=cast, world=None)
        rng = random.Random(7)
        for _ in range(12):
            tick_round(world, npcs, rng=rng)
        for n in npcs.values():
            n.save()
        live = fingerprint(world)
        print("【重启前·内存世界】\n ", live)

        # ② 两张卡各自记了什么
        print("\n【盘上两张卡里的 world 快照】")
        same = True
        for name, card in cards(store).items():
            fp = fingerprint(card["world"])
            print(f"  {name}: {fp}")
            same = same and fp == live
        print("  → 两张卡与内存一致:", "✅" if same else "❌")

        # ③ 模拟重启
        world2, _ = load_village(store_dir=str(store), personas=cast, world=None)
        after = fingerprint(world2)
        print("\n【重启后·世界】\n ", after)
        print("  → 与重启前一致:", "✅ 一致" if after == live else "❌ 有差异")
        for k in live:
            if after.get(k) != live.get(k):
                print(f"    ↳ {k}: 前 {live.get(k)} → 后 {after.get(k)}")

        # ④ 把「非首位」的卡改旧 —— 连 saved_at 一起改旧（T-02 修复后权威看时间戳）
        target = list(cast)[1]
        p = store / f"{target}_memory.json"
        card = json.loads(p.read_text(encoding="utf-8-sig"))
        card["world"]["delivered"] = {"木材": 0}
        card["world"]["_tick"] = 1
        card["world"]["actors"][target]["position"] = "矿洞"
        card["saved_at"] = "2020-01-01T00:00:00"        # ← 关键：时间戳也退回去
        p.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        world3, _ = load_village(store_dir=str(store), personas=cast, world=None)
        fp3 = fingerprint(world3)
        print(f"\n【把非首位的 {target} 的卡连 saved_at 一起改旧后重启】\n ", fp3)
        print("  → 旧卡有没有把状态带回退:",
              "❌ 被带回退" if fp3["delivered"] != live["delivered"] else "✅ 没有（旧卡不被采纳）")

        # ⑤ 反转角色表顺序 → 权威是否换人（修复前：换 → 整村回退）
        rev = dict(reversed(list(cast.items())))
        world4, _ = load_village(store_dir=str(store), personas=rev, world=None)
        fp4 = fingerprint(world4)
        print("\n【反转角色表顺序重启】\n  顺序:", list(rev), "\n ", fp4)
        print("  → 权威是否随顺序改变:",
              "❗ 是（整村回退）" if fp4 != fp3 else "✅ 否（T-02 修复生效）")

        # ⑥ 边界：只改卡里的内容、不动 saved_at —— 方案②按时间戳判权威，这种卡**仍会被采纳**
        card["world"]["delivered"] = {"木材": 999}      # 手改内容
        card["saved_at"] = "2099-01-01T00:00:00"        # 但时间戳是"最新"
        p.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        world5, _ = load_village(store_dir=str(store), personas=cast, world=None)
        fp5 = fingerprint(world5)
        print("\n【只改内容 + 伪造一个最新 saved_at】\n ", fp5)
        print("  → 是否被采纳:",
              "⚠️ 是 —— 方案②的已知边界：权威跟**时间戳**，不跟内容新鲜度（手改卡的人要自己负责时间戳）"
              if fp5["delivered"] != live["delivered"] else "否")
    finally:
        shutil.rmtree(store, ignore_errors=True)
        print("\n（临时 store 已清理）")


if __name__ == "__main__":
    main()
