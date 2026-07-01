"""
统一工具调用格式 (Unified Tool Call Schema)

所有工具调用必须经过此格式 — 这是架构的核心约束。
格式对齐 OpenAI Codex 的 tool-first 风格：
  { tool, input, reason }

同时支持适配到 OpenAI function-calling、MCP、以及未来协议。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Callable
from enum import Enum


# ═══════════════════════════════════════════════════════
# Core Data Types
# ═══════════════════════════════════════════════════════

class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR   = "error"
    TIMEOUT = "timeout"
    REJECTED = "rejected"   # 被安全检查拒绝


@dataclass
class ToolCall:
    """
    统一工具调用格式 — 系统中唯一的工具调用表示。

    示例:
      ToolCall(
          tool="read_file",
          input={"path": "/foo/bar.py", "lines": "1-50"},
          reason="需要读取目标文件来理解当前实现"
      )
    """
    tool: str                              # 工具名称
    input: Dict[str, Any] = field(default_factory=dict)  # 工具参数
    reason: str = ""                       # 为什么调用此工具 (推理链)
    call_id: str = ""                      # 唯一调用 ID (由 Router 分配)

    # 元数据
    priority: int = 0                      # 优先级 (越大越优先)
    depends_on: List[str] = field(default_factory=list)  # 依赖的 call_id 列表
    timeout_ms: int = 60_000               # 超时 (毫秒)

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
    """
    统一工具结果格式。

    示例:
      ToolResult(
          call_id="tc_001",
          status=ToolResultStatus.SUCCESS,
          data={"content": "import os\\n...", "lines": 50},
      )
    """
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


# ═══════════════════════════════════════════════════════
# Tool Definition (声明式)
# ═══════════════════════════════════════════════════════

@dataclass
class ToolSchema:
    """
    工具的声明式定义 — 包括参数 schema、描述、标签。
    这是工具注册表中每个工具的身份证。
    """
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    # 分类 & 发现
    category: str = "general"              # general / file / code / web / system / memory
    tags: List[str] = field(default_factory=list)

    # 安全
    requires_approval: bool = False        # 是否需要人类批准
    allowed_intents: List[str] = field(default_factory=list)  # 空 = 所有意图允许

    # 执行特征
    is_idempotent: bool = True             # 幂等 (可安全重试)
    is_readonly: bool = True               # 只读 (无副作用)
    estimated_duration_ms: int = 1000       # 预估耗时

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


# ═══════════════════════════════════════════════════════
# Tool Protocol (工具必须实现的接口)
# ═══════════════════════════════════════════════════════

class ToolProtocol:
    """
    所有工具必须实现的协议。

    工具开发者只需:
      1. 继承此类
      2. 实现 schema 属性 (返回 ToolSchema)
      3. 实现 execute 方法
      4. (可选) 实现 validate 方法
    """

    @property
    def schema(self) -> ToolSchema:
        raise NotImplementedError("子类必须实现 schema 属性")

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        """
        前置验证。返回 (通过?, 原因)。
        默认: 总是通过。
        """
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        """
        执行工具。调用前已通过 validate。
        返回 ToolResult。
        """
        raise NotImplementedError("子类必须实现 execute 方法")

    @property
    def name(self) -> str:
        return self.schema.name

    def __repr__(self) -> str:
        return f"<Tool:{self.name}>"
