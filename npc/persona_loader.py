"""
NPCSidekick — 人格 JSON 加载器（制作者放文件即用）。

制作者体验: 往 npcs/ 文件夹放一个 <id>.json, NPC 就活了。
零 Python 代码 — 只改 JSON。

格式示例见 npc/personas/example.json。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

# 已知动作示例（**仅作 UI 建议与文档示例，不参与校验**）
#
# 2026-09-08 变更（Web Console Step 5）: 原先这里是**白名单**，action 不在其中就被丢弃。
# 那等于让加载器替游戏做决定 —— 违反 game-agnostic 红线（"不假设 action"），
# 且与 npc/world.py 的 ACTIONS 长期不一致（这边允许 world 不认识的 rest，
# 却不允许 world 认识的 move/craft/deliver，制作者想写"巡逻"就被拒）。
#
# 现在: action 只要求是**非空字符串**。能不能执行由 Runtime 决定
# （apply_action 对未知动作返回 (world, False, "未知行动: X")，不崩、有明确消息）。
# 想要建议列表请用 console_api 的 GET /api/actions —— 从 Runtime 动态取，不写死在这。
ROUTINE_ACTIONS = ("gather", "rest", "say")

# 必需字段（缺失即报错，防制作者漏填导致 NPC 行为异常）
REQUIRED_FIELDS = ("id", "identity", "personality", "speech_style", "taboos")
# 可选字段（有默认值）
OPTIONAL_FIELDS = ("name", "desires", "goals", "rules", "routine", "system_prompt_override", "context_extra")


def _clean_routine(path_name: str, routine) -> list:
    """校验 routine（自主日常表）。坏结构 → 抛错拒整个文件；坏单项 → 打印警告丢弃。

    制作者友好原则: 手改 JSON 出错时 NPC 顶多"不动"，绝不炸服务。
    """
    if not isinstance(routine, list):
        raise ValueError(f"{path_name} 的 routine 必须是列表")
    cleaned = []
    for i, item in enumerate(routine):
        bad = None
        if not isinstance(item, dict):
            bad = "不是对象"
        else:
            # action 只校验"非空字符串" —— 具体能执行什么由 Runtime 决定，
            # 加载器不替游戏做决定（game-agnostic；见 ROUTINE_ACTIONS 注释）。
            action = item.get("action")
            if not isinstance(action, str) or not action.strip():
                bad = f"action 必须是非空字符串（当前: {action!r}）"
            else:
                for field in ("count", "ticks", "weight"):
                    if field in item and (not isinstance(item[field], (int, float)) or item[field] <= 0):
                        bad = f"{field} 必须是正数"
                        break
        if bad:
            print(f"[persona_loader] 跳过 {path_name}: routine 第 {i + 1} 项（{bad}）")
            continue
        cleaned.append(item)
    return cleaned


def validate_persona_dict(data: Dict, source: str = "persona") -> Dict:
    """校验并清洗一份人格 dict（文件加载 / API 新建共用）。

    返回清洗后的副本（name 兜底、routine 坏项丢弃），坏结构抛 ValueError。
    制作者友好原则: 手改 JSON 出错时 NPC 顶多"不动"，绝不炸服务。
    """
    # 注意: taboos 可以是空列表 []（合法），不能按 falsy 判断缺失
    missing = [f for f in REQUIRED_FIELDS if f not in data or data.get(f) in (None, "")]
    if missing:
        raise ValueError(f"{source} 缺少必需字段: {missing}（必需: {REQUIRED_FIELDS}）")
    cleaned = dict(data)
    if not cleaned.get("name"):
        cleaned["name"] = cleaned["id"]
    if not isinstance(cleaned.get("taboos", []), list):
        raise ValueError(f"{source} 的 taboos 必须是列表")
    if "routine" in cleaned:
        cleaned["routine"] = _clean_routine(source, cleaned["routine"])
    return cleaned


def load_persona_from_json(path: Path) -> Dict:
    """从 JSON 文件加载一个人格。校验必需字段。"""
    # utf-8-sig: 兼容 Windows 工具（记事本/PowerShell）写入的 BOM — 制作者陷阱防御
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return validate_persona_dict(data, source=path.name)


def load_personas_from_dir(directory: str) -> Dict[str, Dict]:
    """扫描目录下所有 *.json 人格文件 → {id: persona}。

    找不到目录/没有 json → 返回空（调用方用默认角色表兜底）。
    """
    d = Path(directory)
    personas: Dict[str, Dict] = {}
    if not d.is_dir():
        return personas
    for f in sorted(d.glob("*.json")):
        try:
            p = load_persona_from_json(f)
            personas[p["id"]] = p
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"[persona_loader] 跳过 {f.name}: {exc}")
    return personas
