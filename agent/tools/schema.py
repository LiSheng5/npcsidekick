"""
统一工具调用格式。

所有工具调用经过此格式，对齐 OpenAI function-calling 及未来协议。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Callable
from enum import Enum


# Core Data Types

class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR   = "error"
    TIMEOUT = "timeout"
    REJECTED = "rejected"   # 被安全检查拒绝


@dataclass
class ToolCall:
    """统一工具调用格式。"""

    tool: str
    input: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    call_id: str = ""
    priority: int = 0
    depends_on: List[str] = field(default_factory=list)
    timeout_ms: int = 60_000

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str | dict) -> "ToolCall":
        if isinstance(data, str):
            data = json.loads(data)
        return cls(
            tool=data.get("tool", ""),
            input=data.get("input", {}),
            reason=data.get("reason", ""),
            call_id=data.get("call_id", ""),
            priority=data.get("priority", 0),
            depends_on=data.get("depends_on", []),
            timeout_ms=data.get("timeout_ms", 60_000),
        )

    @classmethod
    def from_openai_tool_call(cls, openai_call) -> "ToolCall":
        """从 OpenAI tool_call 对象转换。"""
        try:
            arguments = json.loads(openai_call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
        return cls(
            tool=openai_call.function.name,
            input=arguments,
            call_id=getattr(openai_call, "id", ""),
        )


@dataclass
class ToolResult:
    """统一工具结果格式。"""

    call_id: str
    tool: str
    status: ToolResultStatus
    data: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "status": self.status.value,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ToolSchema:
    """工具声明式定义。"""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    requires_approval: bool = False
    allowed_intents: List[str] = field(default_factory=list)
    is_idempotent: bool = True
    is_readonly: bool = True
    estimated_duration_ms: int = 1000

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

    def to_dict(self) -> dict:
        return asdict(self)


class ToolProtocol:
    """所有工具必须实现的协议。"""

    @property
    def schema(self) -> ToolSchema:
        raise NotImplementedError

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.schema.name

    def __repr__(self) -> str:
        return f"<Tool:{self.name}>"
