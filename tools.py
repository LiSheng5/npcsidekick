"""工具定义与执行 —— remember / recall + 动作工具（记忆通道 + 动作工具定义）。

两类工具都以标准工具形态进 LLM 上下文（台词纪律：模型只输出角色说的话，不暴露工具痕迹）：
  · remember / recall —— 记忆通道，本模块**直接执行**（memory.py）
  · 动作工具 —— 由 server **同步转发** mod 的 execute_url 执行（协议 §3）；
    本模块只负责"把声明变成工具定义"，不碰 HTTP

白名单即唯一闸门：只有 mod 在 /api/capabilities 声明过的动作才会
变成工具定义，LLM 不可能调用未声明的动作。
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logging_config import log
from core.schema import ToolResult, ToolResultStatus, ToolSchema

import memory

# 记忆工具名（保留字：mod 不能声明同名动作来遮蔽它们）
TOOL_REMEMBER = "remember"
TOOL_RECALL = "recall"
_RESERVED_TOOLS = frozenset({TOOL_REMEMBER, TOOL_RECALL})

# 静默工具：调用完不必再逼模型开口（T4·NPC_SILENT_REMEMBER 开启时可提前收尾）。
# 目前只有 remember —— 它是"顺手记一笔"，不影响台词输出义务。
_SILENT_TOOLS = frozenset({TOOL_REMEMBER})

# 动作参数的声明形态（mod 报到的 params 值，两种写法都认）：
#   · 老写法：字符串 = 说明文字，等价于必填 string
#   · 新写法：对象 {type, enum, desc, optional}，type ∈ string / integer / number / boolean
#     —— optional 真 = 不进 required；enum 写进 schema（不加 additionalProperties，
#     个别网关不认）
_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean"})

REMEMBER_TOOL = ToolSchema(
    name=TOOL_REMEMBER,
    description=("把值得记住的事写进记忆卡。只在确实值得记时才调用（玩家说的重要信息、"
                 "承诺、长期偏好、发生过的关键事件）；寒暄和废话不要记。"
                 "只记玩家明说的话和确实发生过的事，不要记你自己的猜测、推断或对玩家的评价。"
                 "如果【相关记忆】里已经有同一件事（哪怕说法不同），"
                 "用 `replaces` 填它方括号里的编号来更新，不要重复记。"),
    parameters={
        "properties": {
            "content": {"type": "string", "description": "要记住的事，一句话写清关键事实"},
            "importance": {"type": "integer",
                           "description": "重要性 0-9，由你此刻判断（8 以上 = 玩家的正事/长期要求）"},
            "category": {"type": "string",
                         "description": "分类，例如 preference / event / promise"},
            "replaces": {"type": "string",
                         "description": "要更新的旧记忆编号（方括号里的 6 位字符）；没有就不填"},
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


def param_spec_error(value: Any) -> Optional[str]:
    """params 值的形态校验（报到端点用）→ 非法原因文本；None = 合法。"""
    if isinstance(value, str):
        return None
    if not isinstance(value, dict):
        return "必须是字符串或对象"
    ptype = value.get("type")
    if ptype is not None and (not isinstance(ptype, str)
                              or ptype.strip().lower() not in _PARAM_TYPES):
        return "type 必须是 string / integer / number / boolean"
    if value.get("enum") is not None and not isinstance(value.get("enum"), list):
        return "enum 必须是数组"
    if value.get("desc") is not None and not isinstance(value.get("desc"), str):
        return "desc 必须是字符串"
    if value.get("optional") is not None and not isinstance(value.get("optional"), bool):
        return "optional 必须是布尔值"
    return None


def _parse_param_spec(value: Any) -> Tuple[Dict[str, Any], bool]:
    """params 值 → (schema 属性, 是否必填)。两种写法都认（老写法字符串 = 必填 string）。"""
    if isinstance(value, dict):
        ptype = str(value.get("type") or "string").strip().lower()
        if ptype not in _PARAM_TYPES:
            ptype = "string"
        prop: Dict[str, Any] = {"type": ptype}
        desc = value.get("desc")
        if isinstance(desc, str) and desc.strip():
            prop["description"] = desc
        enum = value.get("enum")
        if isinstance(enum, list) and enum:
            prop["enum"] = list(enum)
        return prop, not bool(value.get("optional"))
    return {"type": "string", "description": str(value)}, True


def action_tool_schema(action: Dict[str, Any]) -> ToolSchema:
    """把 mod 声明的动作变成工具定义（只认 /api/capabilities 报上来的动作）。

    params 的值两种形态都认：字符串（老写法 = 必填 string + 说明）/
    对象 `{type, enum, desc, optional}`（optional 的不进 required，enum 与 type
    写进 schema；不加 additionalProperties，个别网关不认）。
    """
    params = action.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for key, spec in params.items():
        prop, is_required = _parse_param_spec(spec)
        properties[str(key)] = prop
        if is_required:
            required.append(str(key))
    return ToolSchema(
        name=str(action.get("name", "")),
        description=str(action.get("desc", "")),
        parameters={"properties": properties, "required": required},
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


def is_silent_tool(name: str) -> bool:
    """静默工具：调用完不必再让模型补台词（目前只有 remember）。

    T4 的收尾判断用它区分"顺手记一笔"（可提前收尾）与要出结果的调用
    （recall / 动作，照常继续多轮）。
    """
    return name in _SILENT_TOOLS


def find_action(name: str, capabilities: Optional[List[dict]]) -> Optional[Dict[str, Any]]:
    """按名字取 mod 声明的动作定义（validate_action_args 拿它当校验依据）；没声明 → None。"""
    for action in capabilities or []:
        if str((action or {}).get("name", "")).strip() == name:
            return action
    return None


def build_tool_definitions(capabilities: Optional[List[dict]] = None) -> List[dict]:
    """组装进 LLM 上下文的工具数组：remember + recall + 声明过的动作。

    固定顺序（remember / recall 恒在前，动作按名排序）让工具定义前缀稳定，
    同一套能力反复出现时利于前缀缓存。
    capabilities 为空（mod 离线 / 未报到 / 没给执行接口）→ 只给记忆工具（不给动作工具）。
    """
    tools = [REMEMBER_TOOL.to_openai_function(), RECALL_TOOL.to_openai_function()]
    declared: Dict[str, Dict[str, Any]] = {}
    for action in capabilities or []:
        name = str((action or {}).get("name", "")).strip()
        if name and name not in _RESERVED_TOOLS and name not in declared:
            declared[name] = action          # 同名取先声明的（白名单口径）
    for name in sorted(declared):
        tools.append(action_tool_schema(declared[name]).to_openai_function())
    return tools


# ── 执行前的参数校验（D9）──────────────────────────────────
# 开关 NPC_VALIDATE_ACTION_ARGS（现读可热切）：
#   · 缺必填、类型不符、不在枚举内 → 不下发给 mod，回灌错误让模型重发
#   · integer / number / boolean 接受可无损转换的字符串并转型
#   · string 接受数字（3 → "3"、2.5 → "2.5"）；bool 与其他类型仍拒绝（T10）
#   · 未声明的多余参数丢弃并记警告，不拒绝

def validate_action_args(action: Dict[str, Any],
                         args: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """按声明校验并转型工具参数 → (清洗后的参数, 错误文本)；错误文本 None = 合法。

    失败时错误文本给模型重发（调用方据此不调 execute_url）；成功时多余参数已丢弃、
    字符串已按声明转型（D9 口径）。
    """
    params = action.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    raw = args if isinstance(args, dict) else {}
    for key in raw:
        if key not in params:
            log.warning("action_args_extra_dropped", action=str(action.get("name", "")),
                        param=str(key)[:40])
    cleaned: Dict[str, Any] = {}
    errors: List[str] = []
    for key, spec in params.items():
        prop, required = _parse_param_spec(spec)
        if key not in raw or raw[key] is None:
            if required:
                errors.append(f"缺少必填参数 {key}")
            continue
        value, err = _coerce_arg(raw[key], prop, str(key))
        if err:
            errors.append(err)
        else:
            cleaned[key] = value
    return (cleaned, None) if not errors else (raw, "；".join(errors))


def _coerce_arg(value: Any, prop: Dict[str, Any], key: str) -> Tuple[Any, Optional[str]]:
    """单个参数按声明的 type / enum 校验并转型 → (值, 错误)。"""
    ptype = prop.get("type", "string")
    out: Any
    if ptype == "string":
        # T10：string 参数收到数字（`note: 3`）时无损转字符串，别为这个多花一轮。
        # bool 仍拒绝 —— 它是 int 的子类，转成 "True" 语义不明；其余类型照旧拒绝。
        if isinstance(value, bool):
            return None, f"参数 {key} 必须是字符串"
        if isinstance(value, str):
            out = value
        elif isinstance(value, int):
            out = str(value)
        elif isinstance(value, float) and math.isfinite(value):
            out = str(value)
        else:
            return None, f"参数 {key} 必须是字符串"
    elif ptype == "integer":
        out = _as_integer(value)
        if out is None:
            return None, f"参数 {key} 必须是整数"
    elif ptype == "number":
        out = _as_number(value)
        if out is None:
            return None, f"参数 {key} 必须是数字"
    else:                                       # boolean
        out = _as_boolean(value)
        if out is None:
            return None, f"参数 {key} 必须是布尔值"
    enum = prop.get("enum")
    if isinstance(enum, list) and enum and out not in enum:
        return None, f"参数 {key} 不在允许的取值内"
    return out, None


def _as_integer(value: Any) -> Optional[int]:
    """→ 整数（bool 不算；浮点/字符串仅无损可转时转）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _as_number(value: Any) -> Optional[float]:
    """→ 数字（bool 不算；拒收 nan / inf）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _as_boolean(value: Any) -> Optional[bool]:
    """→ 布尔（字符串只认 true / false —— 无损可转的那两个）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    return None


def run_tool(name: str, args: Dict[str, Any], npc_id: str,
             capabilities: Optional[List[dict]] = None,
             top_k: int = 5,
             synonyms: Optional[Dict[str, Any]] = None,
             store_dir: Optional[Path] = None) -> ToolResult:
    """执行一次**非动作**工具调用（server 侧只把记忆工具送到这里）。

    remember / recall 由本模块执行；动作工具走 server 的同步转发（协议 §3），
    误送进来只返回 REJECTED；未知工具返回 ERROR（不抛异常）。
    """
    started = time.perf_counter()
    args = args if isinstance(args, dict) else {}

    if name == TOOL_REMEMBER:
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return _result(name, ToolResultStatus.ERROR, error="remember 缺少 content", started=started)
        # importance / category 仅在模型显式给了才传（replaces 更新时没给就保留原值）
        extra: Dict[str, Any] = {}
        if args.get("importance") is not None:
            extra["importance"] = _as_importance(args.get("importance"))
        raw_cat = args.get("category")
        if isinstance(raw_cat, str) and raw_cat.strip():
            extra["category"] = raw_cat
        raw_rep = args.get("replaces")
        replaces = raw_rep.strip() if isinstance(raw_rep, str) and raw_rep.strip() else None
        entry, updated = memory.remember_entry(npc_id, content, replaces=replaces,
                                               store_dir=store_dir, **extra)
        if updated:
            data = "已更新旧记忆"
        elif replaces:
            data = (f"没找到要更新的旧记忆，已当新记忆记下（重要性 {entry['importance']}）")
        else:
            data = f"已记住（重要性 {entry['importance']}）"
        return _result(name, ToolResultStatus.SUCCESS, data=data, started=started)

    if name == TOOL_RECALL:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _result(name, ToolResultStatus.ERROR, error="recall 缺少 query", started=started)
        entries = memory.retrieve(npc_id, query, top_k=top_k, synonyms=synonyms,
                                  store_dir=store_dir)
        log.debug("recall_done", npc_id=npc_id, hits=len(entries))
        return _result(name, ToolResultStatus.SUCCESS,
                       data=memory.format_for_context(entries), started=started)

    if is_action_tool(name, capabilities):
        # 动作执行由 server 同步转发 mod 的 execute_url（协议 §3）；本模块不碰 HTTP
        return _result(name, ToolResultStatus.REJECTED,
                       error="动作由 server 转发给 mod 执行，本模块不代劳", started=started)

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
        tool=name,
        status=status,
        data=data,
        error=error,
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
