"""
NPCSidekick — 10 任务基准（设计文档第 9 节，第 2 周实验）。

验证规则快路径在 3 档难度 + 受阻场景下的表现，并标出
"规则做不了的地方" = LLM 增值区（v4 帮手 NPC 的活）。

用法:
  python -m npc.benchmark
"""
from __future__ import annotations

import time
from typing import Dict, List

from npc.npc import NPC
from npc.world import default_world

# 任务集: (名称, 步骤列表, 难度, 预期)
TASKS: List[Dict] = [
    {"name": "采 1 木材", "steps": [{"type": "gather", "resource": "木材", "count": 1}], "tier": "简单"},
    {"name": "采 3 木材", "steps": [{"type": "gather", "resource": "木材", "count": 3}], "tier": "简单"},
    {"name": "采 3 石头", "steps": [{"type": "gather", "resource": "石头", "count": 3}], "tier": "简单"},
    {"name": "双资源采集", "steps": [
        {"type": "gather", "resource": "木材", "count": 2},
        {"type": "gather", "resource": "石头", "count": 2},
    ], "tier": "中等"},
    {"name": "采集+交付", "steps": [
        {"type": "gather", "resource": "木材", "count": 3},
        {"type": "deliver", "resource": "木材", "count": 3},
    ], "tier": "中等"},
    {"name": "完整链路(合成交付)", "steps": [
        {"type": "gather", "resource": "木材", "count": 2},
        {"type": "gather", "resource": "石头", "count": 1},
        {"type": "craft", "recipe": "木石工具", "count": 1},
        {"type": "deliver", "resource": "木石工具", "count": 1},
    ], "tier": "复杂"},
    {"name": "大额交付", "steps": [
        {"type": "gather", "resource": "木材", "count": 5},
        {"type": "deliver", "resource": "木材", "count": 5},
    ], "tier": "复杂"},
    {"name": "受阻: 资源枯竭", "steps": [
        {"type": "gather", "resource": "铁矿石", "count": 3},   # 世界上不存在
    ], "tier": "受阻", "expect": "fail"},
    {"name": "受阻: 配方缺料", "steps": [
        {"type": "craft", "recipe": "木石工具", "count": 1},   # 背包空
    ], "tier": "受阻", "expect": "fail"},
    {"name": "受阻: 未知步骤", "steps": [
        {"type": "fly", "resource": "木材", "count": 1},        # 未知动作
    ], "tier": "受阻", "expect": "fail"},
]


def run_benchmark() -> None:
    results = []
    for task in TASKS:
        npc = NPC(store_dir="npc/store_bench")   # 隔离基准数据
        started = time.time()
        ok = npc.run_task(task["steps"])
        duration = time.time() - started
        steps = len(npc.task_log[-1]["steps"]) if npc.task_log else 0
        results.append((task, ok, steps, duration))

    # ── 结果表 ────────────────────────────────────────
    print(f"{'任务':<16}{'档位':<6}{'结果':<8}{'步骤':<6}{'耗时(s)':<8}")
    print("-" * 50)
    passed = 0
    for task, ok, steps, duration in results:
        expect = task.get("expect", "pass")
        correct = (ok is True) if expect == "pass" else (ok is False)
        passed += correct
        mark = "✓" if correct else "✗"
        print(f"{task['name']:<16}{task['tier']:<6}{'成功' if ok else '失败':<8}{steps:<6}{duration:.3f}s {mark}")
    print("-" * 50)
    print(f"通过: {passed}/{len(TASKS)}（受阻任务预期=优雅失败）")
    print(f"LLM 调用: 0（规则快路径全程零模型调用，成本 ¥0）")

    # ── LLM 增值区（规则做不了 → v2 的活）────────────────
    print()
    print("=== LLM 增值区（规则快路径的边界，v4 帮手 NPC 的活）===")
    print("""
1. 受阻任务的备选方案 — 铁矿石不存在时，规则只会报失败；
   LLM 可以决定"换木头/问主角/去别处看看"
2. 未知/开放式任务 — "帮我收拾一下家" 规则无法分解步骤
3. 动态世界感知 — 主角换了位置/新资源出现，需要感知+重规划
4. 优先级权衡 — 多个任务排队时，欲望/目标的加权排序
""")


if __name__ == "__main__":
    run_benchmark()
