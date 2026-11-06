"""角色卡字段说明的唯一来源。

控制台「角色卡」页经 `/api/state` 取本模块的数据；
`personas/_角色卡说明.md` 的「字段一览」由 `scripts/gen_role_doc.py` 生成。
改字段说明只改下面的 `FIELDS`，不要在前端或文档里再抄一份。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# (字段名, 中文名, 必填程度, 怎么填)
# 描述一律用纯文本、不带 Markdown 标记：同一段文字要同时显示在网页表格和文档表格里。
FIELDS: List[Tuple[str, str, str, str]] = [
    ("id", "编号", "必填",
     "本文件的名字（不含 .json），必须与文件名一致；控制台允许字母（含大写）/数字/"
     "下划线/点/连字符，长度 1–64，不一致时拒绝保存"),
    ("name", "显示名", "必填",
     "游戏里怎么称呼它，例如 哥特贝拉；只用于控制台与关系网展示，不进系统提示词"),
    ("identity", "身份", "推荐",
     "它是谁、干什么的、处在什么处境，一两句话"),
    ("personality", "性格", "推荐",
     "脾气秉性、在意什么、怕什么"),
    ("speech_style", "说话风格", "推荐",
     "长短句、口癖、语气（“短句不废话”比“很酷”管用）"),
    ("voice_samples", "语气锚点", "推荐",
     "写 2~3 句它真会说的话，让模型抓住语气；别写成性格概括"),
    ("taboos", "忌讳", "可选",
     "它绝不会做的事、不会说的话"),
    ("rules.replies", "关键词兜底", "可选",
     "没接上大模型时，玩家话里包含某关键词就直接回这句；按 JSON 里的先后顺序，"
     "命中第一条就用它"),
    ("rules.fallback", "万能兜底", "可选",
     "没接上大模型且没命中关键词时说的话"),
    ("entity_synonyms", "同义词族", "可选",
     "同一东西的多种叫法归到一个标准词，供记忆检索；标准词本身也要写进它自己的列表，"
     "如 玫瑰: [玫瑰, 花儿, 花]；不填也能跑"),
    ("voice", "音色", "可选",
     "TTS 音色名（要装 edge-tts 才有声），留空用默认"),
    ("relations", "关系网", "可选",
     "只画控制台关系图，不影响对话；[{who, how, weight}]，who 可以写不存在的 id，"
     "控制台会补成合成节点"),
]

_MD_HEADER = "| 字段 | 中文名 | 必填 | 怎么填 |\n|---|---|---|---|"


def for_api() -> List[Dict[str, Any]]:
    """前端表格用的三列视图（经 `/api/state` 下发）。"""
    return [{"key": key, "cn": cn, "desc": desc} for key, cn, _, desc in FIELDS]


def render_markdown_table() -> str:
    """`personas/_角色卡说明.md` 用的四列表格。"""
    rows = []
    for key, cn, required, desc in FIELDS:
        safe_desc = desc.replace("|", r"\|")
        rows.append(f"| `{key}` | {cn} | {required} | {safe_desc} |")
    return "\n".join([_MD_HEADER, *rows])
