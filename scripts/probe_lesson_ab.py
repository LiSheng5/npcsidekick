#!/usr/bin/env python3
"""A/B 探针：反思 lesson 到底有没有改变决策（2026-09-16 · G1 另一半）。

审计 Phase 6 的要求：「**必带 A/B 测试**（同任务有/无反思对比）」。
本探针给**可复现的机制级证据**（零 LLM、零网络，纯规则抽签）：

  同一世界 / 同一日常表（采木材 + 采浆果，等权）/ 同一批种子 →
  比较四组的"选木材"次数：
    A 无 lesson + 开关关（基线）
    B 有 lesson + 开关关（应当与 A **逐次相同** —— 关 = 零回归）
    C 无 lesson + 开关开（应当与 A 相同 —— 没有经验就没有影响）
    D 有 lesson + 开关开（"避着砍木头"生效 → 应**显著低于** A）

用法:
  python scripts/probe_lesson_ab.py

2026-09-16 实测：A=B=C，D 明显低于 A（少选木材）。见《NPC大脑架构》§30.1 第 5 步。
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 项目根入 path

from npc.npc import NPC                                          # noqa: E402
from npc.scheduler import _choose_routine_item                   # noqa: E402
from npc.world import default_world                              # noqa: E402

SEEDS = range(300)
WOOD = {"action": "gather", "resource": "木材", "count": 1, "weight": 1}
BERRY = {"action": "gather", "resource": "浆果", "count": 1, "weight": 1}


def _npc(pid: str, world: dict, lesson: bool) -> NPC:
    persona = {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [], "rules": {"replies": {}, "fallback": "嗯。"},
        "routine": [dict(WOOD), dict(BERRY)], "goals": {},
    }
    npc = NPC(persona=persona, world=world, store_dir="npc/store_test")
    if lesson:
        npc.memory.add("上次砍木头摔了腿 —— 这类活要谨慎", importance=8,
                       category="reflection", mtype="episodic",
                       scope={"action": "gather", "resource": "木材"}, recommendation=-1)
    return npc


def _picks(npc: NPC, world: dict) -> list:
    return [_choose_routine_item(npc, world, random.Random(s))["resource"] for s in SEEDS]


def main() -> None:
    world = default_world()
    world["actors"] = {}
    world["protagonist"] = {"position": "村庄", "name": "主角"}
    world["_tick"] = 0

    runs = {}
    for label, has_lesson, switch_on in (("A 基线（无 lesson · 开关关）", False, False),
                                         ("B 有 lesson · 开关关", True, False),
                                         ("C 无 lesson · 开关开", False, True),
                                         ("D 有 lesson · 开关开", True, True)):
        os.environ["NPC_LESSONS"] = "1" if switch_on else "0"
        npc = _npc(f"n{label[0]}", world, has_lesson)
        runs[label] = _picks(npc, world)

    base = runs["A 基线（无 lesson · 开关关）"]
    print(f"每组 {len(base)} 次抽签；日常表 = 采木材 + 采浆果（等权）\n")
    print(f"{'组':<26}{'选木材':<8}{'与 A 逐次相同?':<16}")
    print("-" * 52)
    for label, picks in runs.items():
        wood = sum(1 for r in picks if r == "木材")
        same = "—" if label.startswith("A") else ("✅ 是" if picks == base else "❌ 否")
        print(f"{label:<26}{wood:<8}{same:<16}")
    print("-" * 52)
    d = sum(1 for r in runs["D 有 lesson · 开关开"] if r == "木材")
    a = sum(1 for r in base if r == "木材")
    print(f"\n结论：开关关 / 没经验 → 与基线逐次一致；**有经验且开关开 → 选木材 {a} → {d}**"
          f"（降幅 {(a - d) / max(a, 1):.0%}）")
    print("（零 LLM、零网络；同种子可复现）")


if __name__ == "__main__":
    main()
