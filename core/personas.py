"""角色卡：读取 / 枚举 / 编译系统提示词（游戏面与控制台共用的唯一实现）。

约定：
  · 目录由调用方传入 —— 本模块不持有全局路径，也不知道 HTTP 是什么
  · `_` 开头的文件是模板/说明一类非 NPC 文件，不算角色
  · 读取失败统一抛 `PersonaError`（消息已含路径，可直接给用户看）：
    游戏面转 400、控制台按 `missing` 转 404

调用方：`server.py`（游戏面）、`console_api.py`（控制台）、`turn.py`（提示词与规则回复）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class PersonaError(Exception):
    """角色卡读不了：缺文件 / JSON 坏 / 不是 JSON 对象。

    `missing` 供调用方区分"没有这张卡"（控制台回 404）与"卡坏了"（回 400）。
    """

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


def persona_path(persona_dir: Path, npc_id: str) -> Path:
    """一张角色卡的路径（不做存在性检查）。"""
    return persona_dir / f"{npc_id}.json"


def persona_files(persona_dir: Path) -> List[Path]:
    """目录里的角色卡文件（`_` 开头的不算），按文件名排序。"""
    if not persona_dir.exists():
        return []
    return sorted(p for p in persona_dir.glob("*.json") if not p.name.startswith("_"))


def persona_ids(persona_dir: Path) -> List[str]:
    """目录里所有 NPC 的 id（同样排除 `_` 开头的模板/说明）。"""
    return [path.stem for path in persona_files(persona_dir)]


def read_persona(path: Path) -> Dict[str, Any]:
    """读一张角色卡；缺文件 / JSON 坏 / 不是对象 → `PersonaError`。"""
    if not path.exists():
        raise PersonaError(f"未找到 NPC 角色卡: {path}", missing=True)
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise PersonaError(f"角色卡 JSON 损坏: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise PersonaError(f"角色卡必须是 JSON 对象: {path}")
    return data


def build_system_prompt(persona: Dict[str, Any], observation: Any = None) -> str:
    """角色卡 JSON 编译成系统提示词（含台词纪律与"查不到就说不知道"的接地要求）。"""
    lines = [
        f"你是{persona.get('identity') or '一个游戏角色'}。",
        f"性格: {persona.get('personality') or '友善'}。",
        f"说话风格: {persona.get('speech_style') or '自然'}。",
    ]
    samples = persona.get("voice_samples") or []
    if isinstance(samples, list) and samples:
        lines.append("你说过的台词（语气和用词严格按这些来）:")
        lines.extend(f"- 「{s}」" for s in samples)
    taboos = persona.get("taboos") or []
    if isinstance(taboos, list) and taboos:
        lines.append(f"禁忌: 你绝不会{'、'.join(str(t) for t in taboos)}。")

    if observation:
        text = observation if isinstance(observation, str) else json.dumps(
            observation, ensure_ascii=False)
        lines.append(f"当前情况（游戏观测）: {text}")

    lines.extend([
        "规矩:",
        "1. 你只输出角色说的话（台词）。不要出现“调用工具/函数/参数”之类的说法，"
        "也不要写旁白或舞台说明。",
        "2. 动作就是你的工具调用：做不做由你自己判断——该做就直接调对应的工具，"
        "它会在游戏里真的执行，拿到的结果就是事实（做成了就说做成了，没做成别假装）；"
        "判断不该做、做不到或不想做，就别调工具，直接用台词回绝"
        "（回绝也是你的回答，没有别人替你拍板，也不用等谁批准）。"
        "台词用自然语言表达（例如“我去煮饭”），不要描述工具调用本身。",
        "3. 只依据上文出现的事实回答。不知道就说不知道，绝不编造。",
        "4. 涉及往事、答应过的事或记不清的细节时，先查记忆（recall）再按查到的原文回答；"
        "查不到就说不知道，绝不编造。",
    ])
    return "\n".join(lines)


def rule_reply(persona: Dict[str, Any], message: str) -> str:
    """LLM 不可用时的规则回复（按角色卡 rules.replies 的关键词命中，兜底用 rules.fallback）。"""
    rules = persona.get("rules") or {}
    replies = rules.get("replies") or {}
    if isinstance(replies, dict):
        for keyword, reply in replies.items():
            if keyword and keyword in message:
                return str(reply)
    fallback = rules.get("fallback")
    return str(fallback) if fallback else "……"
