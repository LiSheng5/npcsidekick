"""
类型化事件定义 — 对应 Reasonix 的 event.Sink 模式。

所有 Agent 内部状态变化都通过事件记录，不再使用零散的 print()。
事件可被 structlog 消费、测试断言、或未来接入监控系统。

事件分类:
  - Lifecycle: 组件初始化、启动、关闭
  - Planning: 任务计划生成、调整
  - Execution: 步骤执行、工具调用
  - Reflection: 反思决策
  - Error: 各类异常
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ── Event Base ────────────────────────────────────────────


@dataclass
class AgentEvent:
    """所有事件的基类。"""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def event_type(self) -> str:
        return self.__class__.__name__


# ── Lifecycle Events ──────────────────────────────────────


@dataclass
class AgentInitialized(AgentEvent):
    """Agent 初始化完成。"""
    tools_count: int = 0
    model: str = ""
    base_url: str = ""


@dataclass
class ToolsRegistered(AgentEvent):
    """工具注册完成。"""
    tool_names: List[str] = field(default_factory=list)
    count: int = 0


@dataclass
class AgentShutdown(AgentEvent):
    """Agent 正常关闭。"""
    reason: str = ""
    duration_sec: float = 0.0


# ── Planning Events ───────────────────────────────────────


@dataclass
class PlanGenerated(AgentEvent):
    """Planner 生成了任务计划。"""
    task_id: str = ""
    goal: str = ""
    steps_count: int = 0
    estimated_tools: List[str] = field(default_factory=list)
    context_length: int = 0


@dataclass
class PlanReplanTriggered(AgentEvent):
    """触发重新规划。"""
    task_id: str = ""
    failed_step_id: int = 0
    error: str = ""
    reason: str = ""


@dataclass
class PlanCompleted(AgentEvent):
    """计划执行完成。"""
    task_id: str = ""
    total_steps: int = 0
    steps_succeeded: int = 0
    steps_failed: int = 0
    duration_sec: float = 0.0
    compressed: bool = False


# ── Execution Events ──────────────────────────────────────


@dataclass
class StepStarted(AgentEvent):
    """步骤开始执行。"""
    step_id: int = 0
    description: str = ""
    tool: str = ""
    retry_count: int = 0


@dataclass
class StepCompleted(AgentEvent):
    """步骤执行成功。"""
    step_id: int = 0
    duration_ms: float = 0.0
    tool_used: str = ""
    result_size: int = 0  # 结果数据大小 (char count)


@dataclass
class StepFailed(AgentEvent):
    """步骤执行失败。"""
    step_id: int = 0
    error: str = ""
    retry_count: int = 0
    max_retries: int = 0
    duration_ms: float = 0.0


@dataclass
class StepRetrying(AgentEvent):
    """步骤正在重试。"""
    step_id: int = 0
    attempt: int = 0
    wait_seconds: float = 0.0


# ── Tool Call Events ──────────────────────────────────────


@dataclass
class ToolCallStarted(AgentEvent):
    """工具调用开始。"""
    tool_name: str = ""
    step_id: int = 0
    input_summary: str = ""


@dataclass
class ToolCallCompleted(AgentEvent):
    """工具调用完成。"""
    tool_name: str = ""
    status: str = ""  # success / error / rejected / timeout
    duration_ms: float = 0.0
    output_size: int = 0


# ── Reflection Events ─────────────────────────────────────


@dataclass
class ReflectionEvaluated(AgentEvent):
    """Reflector 评估完成。"""
    decision: str = ""  # continue / retry / replan / stop / ask_user
    reason: str = ""
    used_llm: bool = False
    step_id: int = 0


@dataclass
class ReflectionFallback(AgentEvent):
    """Reflector 回退到规则判断。"""
    step_id: int = 0
    reason: str = ""  # LLM unavailable / parse error / config disabled


# ── Memory Events ─────────────────────────────────────────


@dataclass
class ContextCompressed(AgentEvent):
    """上下文压缩完成。"""
    messages_compressed: int = 0
    before_tokens: int = 0
    after_tokens: int = 0


@dataclass
class MemoryRetrieved(AgentEvent):
    """记忆检索完成。"""
    query: str = ""
    stm_hits: int = 0
    ltm_facts: int = 0
    ltm_learnings: int = 0
    fallback_used: bool = False


@dataclass
class MessageAdded(AgentEvent):
    """消息添加到记忆。"""
    role: str = ""
    content_length: int = 0


# ── Error Events ──────────────────────────────────────────


@dataclass
class LLMCallFailed(AgentEvent):
    """LLM 调用失败。"""
    context: str = ""  # planner / executor / reflector / compressor
    error: str = ""
    retryable: bool = True


@dataclass
class ToolExecutionError(AgentEvent):
    """工具执行时发生异常。"""
    tool_name: str = ""
    step_id: int = 0
    error: str = ""
    traceback: str = ""


@dataclass
class ConfigError(AgentEvent):
    """配置错误。"""
    key: str = ""
    message: str = ""
    default_used: Any = None


# ── Streaming Events ────────────────────────────────────────


@dataclass
class StreamingStarted(AgentEvent):
    """流式执行开始。"""
    task: str = ""
    stream_id: str = ""


@dataclass
class ChunkReceived(AgentEvent):
    """收到一个 LLM 流式 chunk。"""
    chunk_index: int = 0
    content: str = ""
    has_tool_call: bool = False
    finish_reason: Optional[str] = None


@dataclass
class StreamingEnded(AgentEvent):
    """流式执行结束。"""
    reason: str = ""  # "completed" | "interrupted" | "error"
    total_chunks: int = 0
    duration_sec: float = 0.0
    answer: str = ""


@dataclass
class StreamError(AgentEvent):
    """流式执行中发生错误。"""
    error: str = ""
    chunk_index: int = 0
    recoverable: bool = False
