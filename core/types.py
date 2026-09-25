"""
流式数据类型 — AsyncGenerator 管道的数据契约。

StreamChunk: LLM 流式响应的最小单位
StreamEvent: Orchestrator 管道的高层事件
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class StreamEventType(str, Enum):
    """流式事件类型 — 对应 orchestrator 管道的不同阶段。"""
    THINKING = "thinking"        # 正在思考/规划
    PLAN_READY = "plan_ready"    # 计划生成完成
    STEP_START = "step_start"    # 步骤开始执行
    STEP_PROGRESS = "step_progress"  # 步骤执行中
    TOOL_CALL = "tool_call"      # 工具调用
    TOOL_RESULT = "tool_result"  # 工具结果
    TEXT_DELTA = "text_delta"    # LLM 文本增量
    STEP_DONE = "step_done"      # 步骤完成
    REFLECTION = "reflection"    # 反思结果
    SYNTHESIS = "synthesis"      # 最终合成中
    DONE = "done"                # 全部完成
    ERROR = "error"              # 错误


@dataclass
class StreamChunk:
    """
    LLM 流式响应的单个 chunk。

    对应 OpenAI streaming API 的一个 delta:
      - content: 文本增量 (None 表示无文本)
      - tool_call_delta: 工具调用增量 (None 表示无工具调用)
      - finish_reason: 终止原因 (最后一个 chunk 才有值)

    使用示例:
      async for chunk in client.stream(messages):
          if chunk.has_content:
              print(chunk.content, end="", flush=True)
          if chunk.has_tool_call:
              ...
    """
    content: str = ""
    tool_call_delta: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    model: str = ""
    index: int = 0  # chunk 序号

    @property
    def has_content(self) -> bool:
        return bool(self.content)

    @property
    def has_tool_call(self) -> bool:
        return self.tool_call_delta is not None

    @property
    def is_final(self) -> bool:
        return self.finish_reason is not None

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "tool_call_delta": self.tool_call_delta,
            "finish_reason": self.finish_reason,
            "model": self.model,
            "index": self.index,
        }

    def __repr__(self) -> str:
        parts = []
        if self.has_content:
            parts.append(f"content={self.content[:40]}...")
        if self.has_tool_call:
            parts.append("tool_delta=...")
        if self.finish_reason:
            parts.append(f"finish={self.finish_reason}")
        return f"<StreamChunk [{self.index}] {' '.join(parts) or 'empty'}>"


@dataclass
class StreamEvent:
    """
    Orchestrator 管道的高层事件。

    与 StreamChunk 的区别:
      - StreamChunk: LLM 原始输出 (token 级别)
      - StreamEvent: 业务逻辑事件 (步骤级别)

    display.py 消费此事件来更新 Rich Live 界面。
    """
    type: StreamEventType
    data: Any = None
    message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def thinking(cls, message: str = "正在思考...", **meta) -> "StreamEvent":
        return cls(type=StreamEventType.THINKING, message=message, metadata=meta)

    @classmethod
    def plan_ready(cls, goal: str, steps_count: int = 0, **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.PLAN_READY,
            message=f"计划: {goal}",
            data={"goal": goal, "steps_count": steps_count},
            metadata=meta,
        )

    @classmethod
    def step_start(cls, step_id: int, description: str, tool: str = "", **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.STEP_START,
            message=f"步骤 {step_id}: {description}",
            data={"step_id": step_id, "description": description, "tool": tool},
            metadata=meta,
        )

    @classmethod
    def tool_call(cls, tool_name: str, step_id: int = 0, **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.TOOL_CALL,
            message=f"调用工具: {tool_name}",
            data={"tool_name": tool_name, "step_id": step_id},
            metadata=meta,
        )

    @classmethod
    def tool_result(cls, tool_name: str, ok: bool, step_id: int = 0, **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.TOOL_RESULT,
            message=f"{'✓' if ok else '✗'} {tool_name}",
            data={"tool_name": tool_name, "ok": ok, "step_id": step_id},
            metadata=meta,
        )

    @classmethod
    def text_delta(cls, content: str, **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.TEXT_DELTA,
            data={"content": content},
            metadata=meta,
        )

    @classmethod
    def step_done(cls, step_id: int, success: bool, **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.STEP_DONE,
            message=f"步骤 {step_id}: {'完成' if success else '失败'}",
            data={"step_id": step_id, "success": success},
            metadata=meta,
        )

    @classmethod
    def reflection(cls, decision: str, reason: str = "", **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.REFLECTION,
            message=f"反思: {decision}",
            data={"decision": decision, "reason": reason},
            metadata=meta,
        )

    @classmethod
    def error(cls, message: str, exception: Optional[Exception] = None, **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.ERROR,
            message=message,
            data={"error": str(exception) if exception else message},
            metadata=meta,
        )

    @classmethod
    def done(cls, answer: str = "", duration_sec: float = 0.0, **meta) -> "StreamEvent":
        return cls(
            type=StreamEventType.DONE,
            message="完成",
            data={"answer": answer, "duration_sec": duration_sec},
            metadata=meta,
        )

    def __repr__(self) -> str:
        return f"<StreamEvent {self.type.value}: {self.message[:60]}>"
