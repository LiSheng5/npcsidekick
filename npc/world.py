"""
NPCSidekick — 世界契约 + 文本世界参考实现。

世界契约（用户实现游戏适配器时按此语义）:
  - 世界状态: 纯 JSON dict（可序列化/可编辑/可存档 — 设计点 #4 记忆=可编辑文档）
  - 感知:    observe(world, who) -> str   把世界状态转成该角色能看到的文本
  - 行动:    apply_action(world, action, params, who) -> (world, ok, message)
            行动集: move / gather / deliver / say

文本世界是参考实现 — 用户抄着改成自己的游戏世界即可。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Dict, List, Tuple

# ── 行动集（契约）───────────────────────────────────
ACTIONS = ("move", "gather", "craft", "deliver", "say")


def default_world() -> Dict:
    """创建默认文本世界（参考世界）。

    多 NPC 设计: actors 是角色槽（{id: {position, inventory}}），
    NPC 实例注册自己的槽位 — 所有 NPC 共享同一个世界（AI Town 模式）。
    """
    return {
        "_tick": 0,           # 自主循环推进的 tick 计数（下划线 = 扩展字段，非契约语义）
        "_resource_caps": {"木材": 999, "浆果": 999, "石头": 999},   # 资源再生上限（旧记忆卡缺失时兜底 999）
        "actors": {},         # NPC 角色槽: {id: {position, inventory}}
        "protagonist": {"position": "村庄", "name": "主角"},
        "locations": {
            "村庄": {
                "desc": "主角居住的小村庄，安静祥和。",
                "resources": {},
                "exits": ["森林", "矿洞", "河边"],
            },
            "森林": {
                "desc": "树木茂密，空气里是松脂的味道。",
                "resources": {"木材": 999},
                "exits": ["村庄"],
            },
            "矿洞": {
                "desc": "阴暗潮湿，石壁上有矿灯的光芒。",
                "resources": {"石头": 999},
                "exits": ["村庄"],
            },
            "河边": {
                "desc": "河水清浅，岸边长着浆果丛。",
                "resources": {"浆果": 999},
                "exits": ["村庄"],
            },
        },
        "delivered": {},          # 已交付给主角的物资 {资源: 数量}
        "recipes": {              # 合成配方（在村庄工作台制作）
            "木石工具": {"木材": 2, "石头": 1, "produces": "木石工具"},
            "结实麻绳": {"木材": 1, "石头": 1, "produces": "结实麻绳"},
        },
        "log": [],                # 世界事件日志
    }


def actor_of(world: Dict, who: str) -> Dict:
    """取角色槽（NPC 注册时自动创建）。"""
    return world["actors"].setdefault(who, {"position": "村庄", "inventory": {}})


def observe(world: Dict, who: str = "cang") -> str:
    """感知: 返回 who 角色当前能看到的文本描述。"""
    actor = actor_of(world, who)
    pos = actor["position"]
    loc = world["locations"][pos]

    lines = [f"你位于{pos}。{loc['desc']}"]
    if loc["resources"]:
        res = "、".join(f"{k} ×{v}" for k, v in loc["resources"].items())
        lines.append(f"这里的资源: {res}")
    if loc["exits"]:
        lines.append(f"可前往: {'、'.join(loc['exits'])}")
    inv = actor["inventory"]
    lines.append(f"你的背包: {('、'.join(f'{k} ×{v}' for k, v in inv.items())) if inv else '空'}")
    for other_id, other in world["actors"].items():
        if other_id != who and other["position"] == pos:
            lines.append(f"{other_id}也在附近。")
    prot = world["protagonist"]
    if prot["position"] == pos:
        lines.append(f"{prot['name']}就在你身边。")
    return "\n".join(lines)


def find_path(world: Dict, start: str, goal: str) -> List[str]:
    """BFS 最短路径: 沿 exits 图寻路（AI Town 用 A* 同理，文本世界 BFS 足够）。

    返回途经地点列表（不含起点）；不可达返回 None。
    """
    from collections import deque

    if start == goal:
        return []
    if goal not in world["locations"]:
        return None
    visited = {start}
    queue = deque([(start, [])])
    while queue:
        pos, path = queue.popleft()
        for nxt in world["locations"][pos]["exits"]:
            if nxt in visited:
                continue
            new_path = path + [nxt]
            if nxt == goal:
                return new_path
            visited.add(nxt)
            queue.append((nxt, new_path))
    return None


def apply_action(world: Dict, action: str, params: Dict, who: str = "cang") -> Tuple[Dict, bool, str]:
    """行动: 改变世界状态，返回 (新世界, 是否成功, 结果消息)。

    契约语义:
      - move(dest)     必须是从当前地点可到达的目的地
      - gather(res)    当前地点必须产该资源 → 背包 +1
      - deliver(res)   必须在主角身边 → 背包 -1，delivered +1
      - say(text)      记录发言到世界日志
    """
    if action not in ACTIONS:
        return world, False, f"未知行动: {action}（可用: {'、'.join(ACTIONS)}）"

    actor = actor_of(world, who)
    pos = actor["position"]
    loc = world["locations"][pos]

    if action == "move":
        dest = params.get("dest", "")
        if dest not in loc["exits"]:
            return world, False, f"无法从{pos}前往{dest}（可前往: {'、'.join(loc['exits'])}）"
        actor["position"] = dest
        world["log"].append(f"{who} 前往 {dest}")
        return world, True, f"你来到了{dest}。{world['locations'][dest]['desc']}"

    if action == "gather":
        resource = params.get("resource", "")
        if resource not in loc["resources"]:
            return world, False, f"{pos}没有资源 {resource}"
        if loc["resources"][resource] <= 0:
            return world, False, f"{pos}的{resource}已采尽"
        loc["resources"][resource] -= 1
        actor["inventory"][resource] = actor["inventory"].get(resource, 0) + 1
        world["log"].append(f"{who} 采集了 1 个{resource}")
        return world, True, f"你采集了 1 个{resource}（背包现有 {actor['inventory'][resource]}）"

    if action == "craft":
        recipe_name = params.get("recipe", "")
        recipe = world.get("recipes", {}).get(recipe_name)
        if recipe is None:
            return world, False, f"没有配方: {recipe_name}"
        if pos != "村庄":
            return world, False, "工作台在村庄，需要回到村庄才能制作"
        inv = actor["inventory"]
        for ing, need in recipe.items():
            if ing == "produces":
                continue
            if inv.get(ing, 0) < need:
                return world, False, f"材料不足: 需要{ing} ×{need}（背包有{inv.get(ing, 0)}）"
        for ing, need in recipe.items():
            if ing == "produces":
                continue
            inv[ing] -= need
        product = recipe["produces"]
        inv[product] = inv.get(product, 0) + 1
        world["log"].append(f"{who} 制作了 {product}")
        return world, True, f"你制作了 1 个{product}（背包现有 {inv[product]}）"

    if action == "deliver":
        resource = params.get("resource", "")
        if actor["inventory"].get(resource, 0) <= 0:
            return world, False, f"背包里没有{resource}"
        prot = world["protagonist"]
        if prot["position"] != pos:
            return world, False, f"{prot['name']}不在这里，无法交付"
        actor["inventory"][resource] -= 1
        world["delivered"][resource] = world["delivered"].get(resource, 0) + 1
        world["log"].append(f"{who} 将 {resource} 交给了 {prot['name']}")
        return world, True, f"你已将 1 个{resource}交给{prot['name']}（累计 {world['delivered'][resource]}）"

    if action == "say":
        text = params.get("text", "")
        world["log"].append(f"{who} 说: {text}")
        return world, True, f"你说: {text}"

    return world, False, f"行动 {action} 参数错误"
