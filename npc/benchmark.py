"""
NPCSidekick — 10 任务基准（设计文档第 9 节，第 2 周实验）。

验证规则快路径在 3 档难度 + 受阻场景下的表现，并标出
"规则做不了的地方" = LLM 增值区（v4 帮手 NPC 的活）。

## P-11 指标化（2026-09-16）

原来这个脚本只会 `print`（无断言 / 无退出码 / 测不到自主 scheduler —— 见审计 V-04）。现在：

- `run_benchmark()` 返回**结构化结果**（dict，可直接 `json.dumps`），含
  ① 任务路径（`NPC.run_task`：3 档难度 + 3 个受阻场景）
  ② **自主循环指标**（`scheduler.tick_round` 纯规则跑 N 帧；零 LLM）
- `main()` 出表（人看）或 `--json`（机器/CI 看），**并按结果给退出码**：
  任何一项与预期不符 → 非零（CI 靠这个卡回归）。
- 零 LLM / 零网络：`llm_calls` 恒 0，跑在 CI 里不需要任何 key。

用法:
  python -m npc.benchmark                 # 人看的表 + 退出码
  python -m npc.benchmark --json          # 机器看（CI 消费）
  python -m npc.benchmark --rounds 40     # 自主循环多跑几帧
  python -m npc.benchmark --store /tmp/b  # 隔离基准数据目录（默认 npc/store_bench）
"""
from __future__ import annotations

import argparse
import json
import random
import time
from typing import Dict, List, Optional

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

DEFAULT_STORE = "npc/store_bench"
DEFAULT_ROUNDS = 24
DEFAULT_SEED = 7


# ── ① 任务路径（保持原样：run_task + 三档难度 + 受阻场景）──────────────

def _run_tasks(store_dir: str) -> List[Dict]:
    out: List[Dict] = []
    for task in TASKS:
        npc = NPC(store_dir=store_dir)              # 隔离基准数据
        started = time.time()
        ok = npc.run_task(task["steps"])
        duration = time.time() - started
        steps = len(npc.task_log[-1]["steps"]) if npc.task_log else 0
        expect = task.get("expect", "pass")
        correct = (ok is True) if expect == "pass" else (ok is False)
        out.append({
            "name": task["name"], "tier": task["tier"], "expect": expect,
            "ok": bool(ok), "correct": bool(correct),
            "steps": steps, "duration_s": round(duration, 4), "llm_calls": 0,
        })
    return out


def _by_tier(tasks: List[Dict]) -> Dict[str, Dict]:
    tiers: Dict[str, Dict] = {}
    for t in tasks:
        slot = tiers.setdefault(t["tier"], {"total": 0, "correct": 0})
        slot["total"] += 1
        slot["correct"] += int(t["correct"])
    for slot in tiers.values():
        slot["pass_rate"] = round(slot["correct"] / slot["total"], 4)
    return tiers


# ── ② 自主循环指标（P-11 新增：原脚本测不到 scheduler）────────────────

def _run_autonomy(store_dir: str, rounds: int, seed: int) -> Dict:
    """纯规则跑 N 帧自主循环（零 LLM），出**机制指标**。

    这是"mock 机制指标"的一半：不评"活干得好不好"（那要 LLM），只量化
    "日常回路是否在转、干了什么、失败几次" —— 固定 seed 下**确定性**，可当回归锚。
    """
    from npc.persona import SAMPLE_NPCS
    from npc.scheduler import tick_round

    world = default_world()
    world["actors"] = {}
    world["protagonist"] = {"position": "村庄", "name": "主角"}
    world["_tick"] = 0
    pid, persona = next(iter(SAMPLE_NPCS.items()))
    npc = NPC(persona=persona, world=world, store_dir=store_dir)

    rng = random.Random(seed)
    started, completed, failed, log_lines = 0, 0, 0, 0
    actions: Dict[str, int] = {}
    for _ in range(max(0, int(rounds))):
        before_log = len(world.get("log", []))
        ev = tick_round(world, {pid: npc}, rng=rng).get(pid, {})
        started += "started" in ev
        completed += "completed" in ev
        failed += "failed" in ev
        for line in world.get("log", [])[before_log:]:
            log_lines += 1
            for act in ("采集", "制作", "交给", "前往", "说"):
                if act in line:
                    actions[act] = actions.get(act, 0) + 1
    return {
        "npc": pid,
        "rounds": rounds,
        "seed": seed,
        "ticks": world.get("_tick", 0),
        "started": started, "completed": completed, "failed": failed,
        "delivered": dict(world.get("delivered", {})),
        "log_len": len(world.get("log", [])),
        "action_counts": actions,
        "log_lines": log_lines,
        "llm_calls": 0,
    }


# ── ③ 汇总 + 退出码 ──────────────────────────────────────────────

def run_benchmark(store_dir: str = DEFAULT_STORE, rounds: int = DEFAULT_ROUNDS,
                  seed: int = DEFAULT_SEED) -> Dict:
    """跑完整基准，返回结构化结果（`json.dumps` 友好）。**不 print。**"""
    t0 = time.time()
    tasks = _run_tasks(store_dir)
    autonomy = _run_autonomy(store_dir, rounds, seed)
    correct = sum(1 for t in tasks if t["correct"])
    total = len(tasks)
    summary = {
        "total": total,
        "correct": correct,
        "failed": total - correct,
        "pass_rate": round(correct / total, 4) if total else 0.0,
        "duration_s": round(time.time() - t0, 4),
        "llm_calls": sum(t["llm_calls"] for t in tasks) + autonomy["llm_calls"],
    }
    return {"summary": summary, "tiers": _by_tier(tasks), "tasks": tasks,
            "autonomy": autonomy}


def _print_table(result: Dict) -> None:
    print(f"{'任务':<16}{'档位':<6}{'结果':<8}{'步骤':<6}{'耗时(s)':<8}")
    print("-" * 50)
    for t in result["tasks"]:
        mark = "✓" if t["correct"] else "✗"
        print(f"{t['name']:<16}{t['tier']:<6}{'成功' if t['ok'] else '失败':<8}"
              f"{t['steps']:<6}{t['duration_s']:.3f}s {mark}")
    print("-" * 50)
    s = result["summary"]
    print(f"通过: {s['correct']}/{s['total']}（受阻任务预期=优雅失败）  耗时 {s['duration_s']:.3f}s")
    print(f"LLM 调用: {s['llm_calls']}（规则快路径全程零模型调用，成本 ¥0）")

    a = result["autonomy"]
    print()
    print(f"=== 自主循环指标（纯规则 {a['rounds']} 帧，零 LLM；seed={a['seed']} 可复现）===")
    print(f"  日常开始 {a['started']} / 完成 {a['completed']} / 失败 {a['failed']}"
          f"  · tick {a['ticks']} · 日志 {a['log_len']} 行")
    print(f"  动作计数: {a['action_counts']}")

    print()
    print("=== LLM 增值区（规则快路径的边界，v4 帮手 NPC 的活）===")
    print("""
1. 受阻任务的备选方案 — 铁矿石不存在时，规则只会报失败；
   LLM 可以决定"换木头/问主角/去别处看看"
2. 未知/开放式任务 — "帮我收拾一下家" 规则无法分解步骤
3. 动态世界感知 — 主角换了位置/新资源出现，需要感知+重规划
4. 优先级权衡 — 多个任务排队时，欲望/目标的加权排序
""")


def _quiet_logs() -> None:
    """把 structlog 压到 WARNING —— `--json` 时保证 **stdout 只有 JSON**（CI 要能直接解析）。

    注意 `agent.logging_config._LazyLogger` 会在**首次使用**时自行 configure 成 INFO/console，
    所以除了 configure_logging 还得把它的 `_configured` 置位，否则第一条日志就把级别改回去。
    任何异常都吞掉（最坏结果只是日志噪音，不影响基准结果）。
    """
    try:
        from agent.logging_config import _LazyLogger, configure_logging
        configure_logging(level="WARNING", mode="console")
        _LazyLogger._configured = True
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。**返回退出码**（0 = 全部符合预期；非 0 = 有回归）。"""
    parser = argparse.ArgumentParser(prog="python -m npc.benchmark",
                                     description="NPCSidekick 10 任务基准（零 LLM）")
    parser.add_argument("--json", action="store_true",
                        help="输出结构化 JSON（CI 消费；stdout 保证只有 JSON）")
    parser.add_argument("--quiet", action="store_true", help="压掉引擎 INFO 日志")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"自主循环帧数（默认 {DEFAULT_ROUNDS}）")
    parser.add_argument("--store", default=DEFAULT_STORE, help="基准数据目录（隔离用）")
    args = parser.parse_args(argv)

    if args.json or args.quiet:
        _quiet_logs()          # 必须在跑之前：日志由引擎在运行期产生

    result = run_benchmark(store_dir=args.store, rounds=args.rounds)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_table(result)
    return 0 if result["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
