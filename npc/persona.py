"""
NPCSidekick — 人格配置（结构化字段，用户填）。

设计点 #1: 人格 = 结构化字段（身份/性格/说话风格/禁忌/欲望/目标/规则对话/日常）。
框架把这些字段注入系统提示词 — 同时解决引擎"模型默认自己是 Claude"的老问题。

角色单一来源（2026-08-09）: 默认角色表从 npc/personas/*.json 加载 —
制作者改 JSON 即改一切（--adapter 与 --persona-dir 两条路径同源）。
JSON 缺失时回退代码内兜底（_FALLBACK_CAST）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from npc.persona_loader import load_personas_from_dir

# 代码位置锚定的项目根（cwd 无关 — 服务器从任何目录启动都能找到角色 JSON）
_BASE_DIR = Path(__file__).resolve().parents[1]

# 人格字段契约
PERSONA_FIELDS = (
    "identity",       # 身份: 你是谁
    "personality",    # 性格
    "speech_style",   # 说话风格
    "taboos",         # 禁忌: 不会做的事/不会说的话
    "desires",        # 短期欲望（设计点 #2: 用户按游戏填定义）
    "goals",          # 长期目标（设计点 #3: 独立存储，不进压缩器）
    "rules",          # 规则模式对话（无 LLM 时用 — 用户自己配，不用写代码）
    "routine",        # 自主日常活动表（不配 = NPC 静止，只在玩家互动时响应）
    # 扩展口（制作者自加系统/情绪/好感度）:
    "system_prompt_override",  # 完整自定义系统提示词（覆盖默认模板，高级用户）
    "context_extra",           # 额外上下文行（如 "你对主角的好感度: 52" — 自定义状态展示）
)


def build_system_prompt(persona: Dict) -> str:
    """把结构化人格编译成系统提示词（LLM 用）。

    设计点: 人格注入 prompt；欲望/目标随世界状态变化，由 NPC 运行时动态更新。
    """
    lines = [
        f"你是{persona.get('identity', '一个游戏角色')}。",
        f"性格: {persona.get('personality', '友善')}。",
        f"说话风格: {persona.get('speech_style', '自然')}。",
    ]
    taboos = persona.get("taboos", [])
    if taboos:
        lines.append(f"禁忌: 你绝不会{'、'.join(taboos)}。")
    return "\n".join(lines)


# ── 默认角色表（苍/阿黎 — 唯一真相在 npc/personas/*.json）─────────
# 兜底: JSON 缺失（目录被删/未同步）时用这份快照，保证默认服务不空转。
_FALLBACK_CAST: Dict[str, Dict] = {
    "cang": {
        "id": "cang",
        "name": "苍",
        "identity": "部落的老猎手，火塘边的话事人，见过冰河那边的大兽热爱合作",
        "personality": "寡言直接，经验老到，话里都是日子熬出来的道理",
        "speech_style": "短句，不废话，带点长辈的训诫味",
        "taboos": ["追跑得快的猎物", "烧湿柴", "入冬前不备兽皮", "没把握就进冰河"],
        "desires": {
            "入冬前把兽皮晾够": 0.9,
            "教后生认兽道": 0.4,
        },
        "goals": {
            "部落安稳过冬": {"progress": 0, "target": 1},
        },
        "rules": {
            "replies": {
                "狩猎": "别追跑得快的，追跑不动的。老骨头的话，错不了。",
                "柴": "柴火要挑干透的，湿柴烧起来全是烟。",
                "狼": "那只小狼崽……喂熟了就是家人。野兽和人，差的就是一口饭的情分。",
                "冬": "天冷下来之前，把兽皮晾够。冬天不认人，只认准备。",
                "兽": "冰河那边有长毛的大兽，比山还高。我年轻时远远见过一回。",
            },
            "fallback": "嗯，火塘边坐着说。",
        },
        "routine": [
            {"action": "gather", "resource": "木材", "count": 2, "weight": 4},
            {"action": "rest", "ticks": 3, "weight": 2},
            {"action": "gather", "resource": "浆果", "count": 1, "weight": 1},
        ],
    },
    "ali": {
        "id": "ali",
        "name": "阿黎",
        "identity": "部落的采集者，认得营地周围每一处浆果地和野菜根热爱合作",
        "personality": "开朗热情，好奇心重，爱说话，怕无聊",
        "speech_style": "轻快活泼，爱感叹号，说话带认细节的习惯",
        "taboos": ["采青浆果", "根茎不洗就吃", "独自走远"],
        "desires": {
            "看一眼猛犸": 0.8,
            "浆果季节多采几把": 0.6,
        },
        "goals": {
            "雪来之前再搭两个棚子": {"progress": 0, "target": 2},
        },
        "rules": {
            "replies": {
                "浆果": "采浆果要认颜色——红的最甜，青的吃了肚子疼，苍爷爷说的。",
                "根": "根茎挖出来要洗三遍，泥里的小石子能硌掉牙！",
                "猛犸": "猛犸！我还没见过呢，苍爷爷说它们走过地面都在抖……真想看看！",
                "天气": "云压得低低的，怕是要下雨。你衣服晾了没？没晾快收！",
                "营地": "咱们营地越来越像样了！等雪来之前，再搭两个棚子就够过冬啦。",
            },
            "fallback": "嘿！你回来啦！",
        },
        "routine": [
            {"action": "gather", "resource": "浆果", "count": 2, "weight": 4},
            {"action": "rest", "ticks": 2, "weight": 2},
            {"action": "gather", "resource": "木材", "count": 1, "weight": 1},
        ],
    },
}

# 默认角色表（JSON 为准，兜底为快照；路径锚定代码位置，不依赖启动目录）
SAMPLE_NPCS: Dict[str, Dict] = load_personas_from_dir(str(_BASE_DIR / "npc" / "personas")) or _FALLBACK_CAST
# 兼容旧代码取 SAMPLE_NPC（默认 = 苍）
SAMPLE_NPC: Dict = SAMPLE_NPCS.get("cang") or _FALLBACK_CAST["cang"]
