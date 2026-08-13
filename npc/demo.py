"""
NPCSidekick — 演示脚本（示例村庄: 苍 + 阿黎，共享世界）。

用法:
  python -m npc.demo                    # 苍自主采 3 木材交付
  python -m npc.demo --npc ali --task "gather浆果2,deliver浆果2"  # 阿黎采集链
  python -m npc.demo --talk "今天忙吗" [--npc ali]   # 对话（LLM，无 key 自动规则）
  python -m npc.server                  # Web 版（多 NPC + 模式开关）
"""
from __future__ import annotations

import argparse

from npc.npc import NPC
from npc.server import load_village


def main() -> None:
    parser = argparse.ArgumentParser(description="NPCSidekick 演示")
    parser.add_argument("--npc", type=str, default="cang", help="NPC id: cang / ali")
    parser.add_argument("--talk", type=str, help="跟 NPC 说一句话")
    parser.add_argument("--task", type=str, help="任务步骤, 逗号分隔, 如 gather木材2,deliver木材2")
    args = parser.parse_args()

    world, npcs = load_village()
    npc: NPC = npcs.get(args.npc)
    if npc is None:
        print(f"没有 NPC {args.npc}，可用: {list(npcs.keys())}")
        return

    name = npc.persona.get("name")

    if args.talk:
        print(f"你: {args.talk}")
        print(f"{name}: {npc.talk(args.talk)}")
        return

    if args.task:
        # 解析: "gather木材2,gather石头1,craft木石工具,deliver木石工具"
        steps = []
        for part in args.task.split(","):
            for kind in ("gather", "deliver", "craft"):
                if part.startswith(kind):
                    rest = part[len(kind):].strip()
                    if kind in ("gather", "deliver"):
                        resource = "".join(ch for ch in rest if not ch.isdigit())
                        count = int("".join(ch for ch in rest if ch.isdigit()) or 1)
                        steps.append({"type": kind, "resource": resource, "count": count})
                    else:
                        steps.append({"type": "craft", "recipe": rest, "count": 1})
                    break
            else:
                print(f"无法解析步骤: {part}")
                return
        ok = npc.run_task(steps)
        print(f"任务{'成功' if ok else '失败'}")
        for s in npc.task_log[-1]["steps"]:
            print(" ", s)
        return

    # 默认演示: 苍采 3 木材交付
    print(f"=== {name} 醒来 ===")
    print(npc.observe())
    print()
    ok = npc.run_gather_task("木材", 3)
    print(f"\n任务{'成功' if ok else '失败'}，记忆卡: {npc.store_path}")
    print(f"\n村庄全景: {[(n.persona['name'], n.actor_pos) for n in npcs.values()]}")


if __name__ == "__main__":
    main()
