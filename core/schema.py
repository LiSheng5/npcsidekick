"""
统一工具调用格式。

所有工具调用经过此格式，对齐 OpenAI function-calling 及未来协议。
只保留当前运行路径真正用到的三种类型：结果状态 / 结果 / 工具声明。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


# Core Data Types

class ToolResultStatus(str, Enum):
    SUCCESS  = "success"
    ERROR    = "error"
    REJECTED = "rejected"   # 被拒绝（白名单闸门外的调用）


@dataclass
class ToolResult:
    """统一工具结果格式（不携带 call_id —— 回传给 LLM 的 tool_call_id 用的是
    模型自己给的那次调用的 id，由调用方 turn.py 直接取，与本结果对象无关）。"""

    tool: str
    status: ToolResultStatus
    data: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS


@dataclass
class ToolSchema:
    """工具声明式定义（只声明进 LLM 上下文所需的三样 + 分类）。"""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    category: str = "general"

    def to_openai_function(self) -> dict:
        """转换为 OpenAI function-calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters.get("properties", {}),
                    "required": self.parameters.get("required", []),
                },
            },
        }
