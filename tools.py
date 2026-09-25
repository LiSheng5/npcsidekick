"""工具定义与执行 —— remember / recall + 动作工具（设计.md §2、§3.1）。

两条通道严格分离（§2.2 台词纪律）：
  · remember / recall —— 记忆通道，服务器**自己执行**（memory.py）
  · 动作工具 —— 提议通道，服务器**只提议不执行**，由游戏侧执行后回报（协议.md §3）

白名单即唯一闸门（§1）：只有 mod 在 /api/capabilities 声明过的动作才会
变成工具定义，LLM 不可能提议未声明的动作。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from core.logging_config import log
from core.schema import ToolResult, ToolResultStatus, ToolSchema

import memory

# 记忆工具名（保留字：mod 不能声明同名动作来遮蔽它们）
TOOL_REMEMBER = "remember"
TOOL_RECALL = "recall"
_RESERVED_TOOLS = frozenset({TOOL_REMEMBER, TOOL_RECALL})

# 动作工具的参数一律按"字符串 + 声明说明"进 schema（协议.md §1 的 params 形态）
_PARAM_TYPE = "string"

REMEMBER_TOOL = ToolSchema(
    name=TOOL_REMEMBER,
    description=("把值得记住的事写进记忆卡。只在确实值得记时才调用（玩家说的重要信息、"
                 "承诺、长期偏好、发生过的关键事件）；寒暄和废话不要记。"),
    parameters={
        "properties": {
            "content": {"type": "string", "description": "要记住的事，一句话写清关键事实"},
            "importance": {"type": "integer",
                           "description": "重要性 0-9，由你此刻判断（8 以上 = 玩家的正事/长期要求）"},
            "category": {"type": "string",
                         "description": "分类，例如 preference / event / promise"},
        },
        "required": ["content"],
    },
    category="memory",
)

RECALL_TOOL = ToolSchema(
    name=TOOL_RECALL,
    description=("回忆与此前对话、事件有关的记忆。需要确认细节或想不起来时调用，"
                 "返回按相关度排序的原始记忆条目。"),
    parameters={
        "properties": {
            "query": {"type": "string", "description": "要回忆的关键词或问题"},
        },
        "required": ["query"],
    },
    category="memory",
)


def action_tool_schema(action: Dict[str, Any]) -> ToolSchema:
    """把 mod 声明的动作变成工具定义（协议.md §1）。"""
    params = action.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    properties = {
        str(k): {"type": _PARAM_TYPE, "description": str(v)}
        for k, v in params.items()
    }
    return ToolSchema(
        name=str(action.get("name", "")),
        description=str(action.get("desc", "")),
        parameters={"properties": properties, "required": list(properties.keys())},
        category="action",
    )


def declared_action_names(capabilities: Optional[List[dict]]) -> List[str]:
    """声明过的动作名（保留字除外）。"""
    names = []
    for action in capabilities or []:
        name = str((action or {}).get("name", "")).strip()
        if name and name not in _RESERVED_TOOLS:
            names.append(name)
    return names


def is_action_tool(name: str, capabilities: Optional[List[dict]]) -> bool:
    """该工具名是不是 mod 声明过的动作。"""
    return name in declared_action_names(capabilities)


def build_tool_definitions(capabilities: Optional[List[dict]] = None) -> List[dict]:
    """组装进 LLM 上下文的工具数组：remember + recall + 声明过的动作。

    capabilities 为空（mod 离线 / 未报到）→ 只给记忆工具（§6：离线不提议动作）。
    """
    tools = [REMEMBER_TOOL.to_openai_function(), RECALL_TOOL.to_openai_function()]
    for action in capabilities or []:
        name = str((action or {}).get("name", "")).strip()
        if not name or name in _RESERVED_TOOLS:
            continue
        tools.append(action_tool_schema(action).to_openai_function())
    return tools


def run_tool(name: str, args: Dict[str, Any], npc_id: str,
             capabilities: Optional[List[dict]] = None,
             top_k: int = 5,
             synonyms: Optional[Dict[str, Any]] = None) -> ToolResult:
    """执行一次工具调用。

    remember / recall 由服务器执行；动作工具**不执行**（返回 REJECTED，
    由 server 侧生成 action 帧交给游戏）；未知工具返回 ERROR（不抛异常）。
    """
    started = time.perf_counter()
    args = args if isinstance(args, dict) else {}

    if name == TOOL_REMEMBER:
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return _result(name, ToolResultStatus.ERROR, error="remember 缺少 content", started=started)
        importance = _as_importance(args.get("importance"))
        category = args.get("category")
        category = category if isinstance(category, str) and category.strip() else memory.DEFAULT_CATEGORY
        entry = memory.add_entry(npc_id, content, importance=importance, category=category)
        return _result(name, ToolResultStatus.SUCCESS,
                       data=f"已记住（重要性 {entry['importance']}）", started=started)

    if name == TOOL_RECALL:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _result(name, ToolResultStatus.ERROR, error="recall 缺少 query", started=started)
        entries = memory.retrieve(npc_id, query, top_k=top_k, synonyms=synonyms)
        log.debug("recall_done", npc_id=npc_id, hits=len(entries))
        return _result(name, ToolResultStatus.SUCCESS,
                       data=memory.format_for_context(entries), started=started)

    if is_action_tool(name, capabilities):
        # 提议通道：执行权在游戏（协议.md §3），服务器不代劳
        return _result(name, ToolResultStatus.REJECTED,
                       error="动作由游戏侧执行，服务器只提议", started=started)

    return _result(name, ToolResultStatus.ERROR, error=f"未知工具: {name}", started=started)


def _as_importance(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return memory.DEFAULT_IMPORTANCE
    try:
        return max(0, min(9, int(float(raw))))
    except (TypeError, ValueError):
        return memory.DEFAULT_IMPORTANCE


def _result(name: str, status: ToolResultStatus, data: Any = None,
            error: Optional[str] = None, started: float = 0.0) -> ToolResult:
    return ToolResult(
        call_id="",
        tool=name,
        status=status,
        data=data,
        error=error,
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
