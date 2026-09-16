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

import json

from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── 行动集（契约）───────────────────────────────────
ACTIONS = ("move", "gather", "craft", "deliver", "say")

# ── 耐力系统(2026-08-22):动作级消耗,每点=1% 体力池 ——————————————
# 参数 [PLACEHOLDER]:参考"轻活/重活"粗分,后续按真实生理功耗(MET 值)再调比例。
# 归零不死:耐力见底只影响 scheduler 动态权重(强推休息),行动本身不禁止 —
# 与游戏端玩家侧"耗尽禁跑"不同:村民会硬撑(老猎手风格),但很快会去歇。
STAMINA_MAX = 100
STAMINA_COST = {"move": 4, "gather": 10, "craft": 6, "deliver": 2, "say": 0}


def stamina_of(world: Dict, who: str) -> float:
    """读 actor 耐力(旧档无键 → 视为满,向后兼容)。"""
    return float(world["actors"].get(who, {}).get("stamina", STAMINA_MAX))


def _drain_stamina(world: Dict, who: str, action: str) -> None:
    """按动作扣耐力,下限 0。所有行动路径(自主循环+LLM 工具)都经 apply_action,统一在此扣。"""
    cost = STAMINA_COST.get(action, 0)
    if cost <= 0:
        return
    actor = world["actors"][who]
    actor["stamina"] = max(0.0, stamina_of(world, who) - cost)


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
        # T-03(2026-09-16): 制作台地点 + 出生点 —— 由**世界声明**（引擎不硬编码游戏地名）。
        # 未声明 craft_at = 就地可制作；未声明 _default_spawn = 取第一个地点。
        "craft_at": "村庄",
        "_default_spawn": "村庄",
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
    """取角色槽（NPC 注册时自动创建）。

    出生点: 世界可用 _default_spawn 声明(如 GTA 的罗克福山);缺省"村庄"。
    2026-08-22 修: 原来硬编码"村庄" — GTA 世界没有这个地点,新槽 KeyError。
    """
    slot = world["actors"].setdefault(
        # T-03(2026-09-16): 兜底取"世界声明的出生点"，再退到第一个地点 —— 不写死任何地名
        who, {"position": world.get("_default_spawn") or next(iter(world["locations"])),
              "inventory": {}})
    slot.setdefault("stamina", STAMINA_MAX)   # 旧档补键(向后兼容)
    return slot


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
    st = actor.get("stamina", STAMINA_MAX)
    if st < 30:
        lines.append("你浑身发沉,胳膊抬不起来——该歇了。")
    elif st < 60:
        lines.append("你有些喘,体力过半。")
    # 天气/真实时间感知(2026-08-22): 游戏经 /api/talk context 同步;未同步时静默(文本世界自洽)
    weather = world.get("_weather", "")
    if weather == "rain":
        lines.append("天上下着雨,雨点砸在树叶上噼啪响。")
    elif weather == "festival":
        lines.append("今天是部落的节庆日,营地热闹得很。")
    gh = world.get("_game_hour")
    if gh is not None and (gh >= 22 or gh < 6):
        lines.append("夜已经很深了。")
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


def craft_station(world: Dict) -> Optional[str]:
    """制作台所在地点 —— **声明驱动**（T-03 · 2026-09-16）。

    世界用 `craft_at` 声明"在哪能制作"（示例世界 = "村庄"）。
    未声明 / 声明的地点不存在 → `None` = **就地可制作** ——
    引擎层不出现任何游戏地名（game-agnostic 红线，见《NPC大脑架构》§3）。
    """
    station = world.get("craft_at")
    if isinstance(station, str) and station and station in world.get("locations", {}):
        return station
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
        _drain_stamina(world, who, "move")
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
        _drain_stamina(world, who, "gather")
        return world, True, f"你采集了 1 个{resource}（背包现有 {actor['inventory'][resource]}）"

    if action == "craft":
        recipe_name = params.get("recipe", "")
        recipe = world.get("recipes", {}).get(recipe_name)
        if recipe is None:
            return world, False, f"没有配方: {recipe_name}"
        # T-03(2026-09-16): 闸门读世界声明（未声明 = 就地制作），引擎里不再出现游戏地名
        station = craft_station(world)
        if station is not None and pos != station:
            return world, False, f"工作台在{station}，需要回到那里才能制作"
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
        _drain_stamina(world, who, "craft")
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
        _drain_stamina(world, who, "deliver")
        return world, True, f"你已将 1 个{resource}交给{prot['name']}（累计 {world['delivered'][resource]}）"

    if action == "say":
        text = params.get("text", "")
        world["log"].append(f"{who} 说: {text}")
        return world, True, f"你说: {text}"

    return world, False, f"行动 {action} 参数错误"


# ── §18 记忆分层·冷层: world.log 分段归档（2026-08-24）──────────────
# 热数据(画像/人设)常驻上下文、温数据(记忆卡+检索)已有、本函数补冷层:
# 长会话 world.log 无上限疯长(RAM 泄漏, SSE 游标吊在它上面) → 头部段落
# 归档落盘, 内存只留尾部。本体(日志流)只追加不删除 — 归档是搬家不是销毁。
LOG_TAIL_DEFAULT = 500
ARCHIVE_FILENAME = "log_archive.jsonl"


def rotate_world_log(world: Dict, tail: int = LOG_TAIL_DEFAULT,
                     archive_dir: Optional[str] = None) -> int:
    """world.log 超过 tail 条 → 头部段落搬进归档文件，内存只留尾部。返回搬动条数。

    契约（游标兼容是铁律）:
      - world["_log_offset"] 累计已搬走的条数; 逻辑索引 = offset + 物理索引。
        /api/events 用它换算 since/log_count — 旧客户端游标是绝对流位置, 不受轮转影响;
      - 归档行带绝对索引 {"i": 逻辑索引, "text": 原行}, 追加写
        <archive_dir>/log_archive.jsonl — since<offset 的冷数据可从它回放;
      - 任何写失败 → 异常上抛, 调用方记日志后放弃本轮轮转（宁可多占内存,
        绝不丢事件 — 本体只追加的只读语义不破）。
    """
    log = world.setdefault("log", [])
    if tail <= 0 or len(log) <= tail or not archive_dir:
        return 0
    cut = len(log) - tail
    dest = Path(archive_dir)
    dest.mkdir(parents=True, exist_ok=True)      # 失败 → 异常上抛 → 调用方放弃轮转
    offset = int(world.get("_log_offset", 0))
    with (dest / ARCHIVE_FILENAME).open("a", encoding="utf-8") as fh:
        for i, line in enumerate(log[:cut]):
            fh.write(json.dumps({"i": offset + i, "text": line},
                                ensure_ascii=False) + "\n")
    del log[:cut]
    world["_log_offset"] = offset + cut
    return cut


# ── 日志分档（2026-08-28 读侧版, 归档留全量 — 用户 D1 保守裁决）───────────
# 档位: interactive(🌟 玩家在场/交际, 行为日志逐条) / autonomous(🌗 自主动作, 摘要一行)。
# 只改读侧注入口（_behavior_log 的"刚才在干嘛"事实源）; 写侧世界日志 append-only 不动,
# rotate_world_log 归档留全量 — 事实源永不缩。任务书#04(2026-08-28): 挂账关闭 ——
# "压缩日记摘要"已由记忆管家接管(npc/housekeeper.py → compress_archive_log)。
LOG_TIER_INTERACTIVE = "interactive"
LOG_TIER_AUTONOMOUS = "autonomous"
# 自主行判定关键词: 与 _parse_log_line 的事件格式同源（scheduler/world 写入点）
_AUTONOMOUS_HINTS = ("前往", "采集了", "制作了", "休息")
_INTERACTIVE_HINTS = ("说:", "交给了")


def log_tier(line: str) -> str:
    """日志行分档（纯函数, 零 LLM）。未知行保守归 interactive（少记比漏记好）。"""
    if any(h in line for h in _AUTONOMOUS_HINTS):
        return LOG_TIER_AUTONOMOUS
    if any(h in line for h in _INTERACTIVE_HINTS):
        return LOG_TIER_INTERACTIVE
    return LOG_TIER_INTERACTIVE


def summarize_autonomous(lines: list) -> str:
    """自主日志行 → 语言化计数摘要（纯规则, 零 LLM）。

    例: [前往森林×2, 采集了 1 个木材×3] → "采集木材×3 · 前往森林×2"。
    无内容 → ""（调用方不注入）。
    """
    counts: dict = {}
    for line in lines:
        if "采集了" in line:
            res = line.split("采集了", 1)[1].split("个", 1)[-1].strip() or "资源"
            key = f"采集{res}"
        elif "前往" in line:
            key = f"前往{line.split('前往', 1)[1].strip()}"
        elif "制作了" in line:
            key = f"制作{line.split('制作了', 1)[1].strip()}"
        elif "休息" in line:
            key = "休息"
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    parts = [f"{k}×{n}" for k, n in
             sorted(counts.items(), key=lambda kv: -kv[1])]
    return " · ".join(parts)


# ── 任务书#04: 归档压缩(记忆管家接管) ─────────────────────────────
# 连续 autonomous 段(≥N 条, 索引连续)压成一行摘要; interactive 逐行保留。
# 摘要行 text = "[自主摘要]" + 摘要(前缀与摘要间无空格) —— 不匹配任何
# _parse_log_line 事件正则("前往"前必有"·"隔断), 旧客户端事件流零感知。
AUTONOMOUS_COMPRESS_MIN = 3
SUMMARY_PREFIX = "[自主摘要]"


def parse_archive_record(line: str) -> Optional[Dict]:
    """归档行容错解析 → {"i": int, "text": str}。坏行 → None(调用方原样保留)。

    归档记录 schema 的唯一读入口(compressor + events_archive 共用) —
    以后加字段/版本化只改这里。
    """
    try:
        rec = json.loads(line)
        return {"i": int(rec["i"]), "text": str(rec["text"])}
    except Exception:
        return None


def compress_archive_log(archive_dir: Optional[str]) -> int:
    """把归档文件里的连续自主段压成摘要行。返回压缩减少的行数。

    游标契约: _log_offset 逻辑条数不变(压缩只影响盘上归档行数);
    压缩行 {"i": 段首绝对索引, "text": SUMMARY_PREFIX+摘要} — 回放器按
    since<=i<offset 过滤, 摘要行解析不成事件, 冷段回放不炸。
    幂等: 已压缩行(前缀命中)不再参与分组; 坏行原样保留; 先写 .tmp 再替换。
    """
    if not archive_dir:
        return 0
    path = Path(archive_dir) / ARCHIVE_FILENAME
    if not path.exists():
        return 0
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    out: List[str] = []
    reduced = 0
    buf: List[Tuple[int, str]] = []

    def flush() -> None:
        nonlocal reduced
        if not buf:
            return
        if len(buf) >= AUTONOMOUS_COMPRESS_MIN:
            summary = summarize_autonomous([t for _, t in buf])
            text = SUMMARY_PREFIX + (summary or buf[0][1])
            out.append(json.dumps({"i": buf[0][0], "text": text},
                                  ensure_ascii=False))
            reduced += len(buf) - 1
        else:
            for i, t in buf:
                out.append(json.dumps({"i": i, "text": t}, ensure_ascii=False))
        buf.clear()

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        rec = parse_archive_record(line)
        if rec is None:
            flush()                 # 坏行切断当前段
            out.append(line)        # 原样保留, 不炸
            continue
        idx, text = rec["i"], rec["text"]
        if text.startswith(SUMMARY_PREFIX):
            flush()
            out.append(line)        # 已压缩行原样传播(幂等)
            continue
        if log_tier(text) == LOG_TIER_AUTONOMOUS:
            if buf and buf[-1][0] + 1 == idx:
                buf.append((idx, text))
            else:
                flush()
                buf.append((idx, text))
        else:
            flush()
            out.append(line)        # 非自主行逐字保留(不重序列化, 字节保真)
    flush()
    if not reduced:
        return 0
    try:
        tmp = path.with_name(ARCHIVE_FILENAME + ".tmp")
        tmp.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        return 0
    return reduced
