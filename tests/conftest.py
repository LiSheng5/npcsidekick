"""
Pytest fixtures — Mock LLM, Memory, Tools 供所有测试使用。

每个 fixture 返回独立实例，测试之间不共享状态。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

# ── Fake data classes (matching project structure) ─────────


@dataclass
class FakeToolCall:
    """Minimal fake for OpenAI tool_calls."""
    id: str = "fake_call_1"
    type: str = "function"
    function: FakeFunctionCall = field(default_factory=lambda: FakeFunctionCall())


@dataclass
class FakeFunctionCall:
    name: str = "emit_task_plan"
    arguments: str = '{"goal":"test","steps":[]}'


@dataclass
class FakeLLMResponse:
    """Mock LLMClient.chat() return."""
    content: Optional[str] = None
    tool_calls: Optional[List[FakeToolCall]] = None
    finish_reason: str = "stop"
    usage: Optional[dict] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class FakeToolResult:
    """Mock ToolResult for testing."""
    call_id: str = ""
    tool: str = "test_tool"
    status: str = "success"
    data: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "success"


# ── Core Fixtures ──────────────────────────────────────────


@pytest.fixture
def mock_llm_response():
    """Create a standard mock LLM response (no tool calls)."""
    return FakeLLMResponse(
        content="这是标准响应。",
        tool_calls=None,
        finish_reason="stop",
    )


@pytest.fixture
def mock_llm_response_with_tools():
    """Create a mock LLM response with tool calls."""
    return FakeLLMResponse(
        content=None,
        tool_calls=[
            FakeToolCall(
                id="tc_1",
                function=FakeFunctionCall(
                    name="emit_task_plan",
                    arguments=json.dumps({
                        "goal": "测试目标",
                        "steps": [
                            {
                                "step_id": 1,
                                "description": "读取配置文件",
                                "tool": "read_file",
                                "tool_input": {"path": "config.yaml"},
                                "depends_on": [],
                                "success_criteria": "成功读取文件内容",
                                "fallback": "检查路径是否正确",
                            },
                            {
                                "step_id": 2,
                                "description": "分析配置内容",
                                "tool": "",
                                "depends_on": [1],
                                "success_criteria": "得出分析结论",
                                "fallback": "",
                            },
                        ],
                    }),
                ),
            ),
        ],
        finish_reason="tool_calls",
    )


@pytest.fixture
def mock_reflection_response():
    """Mock LLM response for reflector (emit_reflection tool call)."""
    return FakeLLMResponse(
        content=None,
        tool_calls=[
            FakeToolCall(
                id="tc_ref",
                function=FakeFunctionCall(
                    name="emit_reflection",
                    arguments=json.dumps({
                        "decision": "continue",
                        "criteria_met": True,
                        "reason": "步骤已成功完成。",
                    }),
                ),
            ),
        ],
        finish_reason="tool_calls",
    )


@pytest.fixture
def mock_llm_client():
    """Mock LLMClient that returns standard responses."""
    client = MagicMock()
    client.chat.return_value = FakeLLMResponse(
        content="Mock LLM 响应",
        tool_calls=None,
        finish_reason="stop",
    )
    return client


@pytest.fixture
def mock_memory_manager():
    """Mock MemoryManager with minimal behavior."""
    mm = MagicMock()
    mm.retrieve_for_planning.return_value = "## 对话历史 (最近)\n[user] 测试消息"
    mm.get_history_for_context.return_value = "[user] 测试消息"
    mm.retrieve.return_value = {
        "query": "test",
        "short_term": [],
        "long_term": {"facts": [], "learnings": [], "executions": []},
        "summary": "",
    }
    mm.maybe_compress.return_value = 0
    mm.recent_messages.return_value = []
    mm.get_conversation_context.return_value = ""
    return mm


@pytest.fixture
def mock_tool_registry():
    """Mock ToolRegistry with a few fake tools."""
    from agent.tools.schema import ToolSchema

    registry = MagicMock()

    # Setup basic tool schemas
    read_schema = ToolSchema(
        name="read_file",
        description="读取文件内容",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
        tags=["file", "read"],
        category="file",
    )

    write_schema = ToolSchema(
        name="write_file",
        description="写入文件",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        tags=["file", "write"],
        category="file",
    )

    search_schema = ToolSchema(
        name="web_search",
        description="搜索网页",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        tags=["web", "search"],
        category="web",
    )

    registry.list_all.return_value = [
        MagicMock(schema=read_schema),
        MagicMock(schema=write_schema),
        MagicMock(schema=search_schema),
    ]
    registry.to_tool_descriptions.return_value = (
        "- read_file: 读取文件内容\n"
        "- write_file: 写入文件\n"
        "- web_search: 搜索网页"
    )
    registry.validate_call.return_value = None  # No error = valid
    registry.get.return_value = MagicMock()
    registry.get_schema.return_value = read_schema
    registry.__len__ = MagicMock(return_value=3)

    return registry


@pytest.fixture
def mock_tool_router(mock_tool_registry):
    """Mock ToolRouter that returns success results."""
    router = MagicMock()
    router.dispatch.return_value = MagicMock(
        call_id="tc_test",
        tool="test_tool",
        status="success",
        ok=True,
        data={"result": "mock data"},
        error=None,
    )
    router.recommend_for_step.return_value = ["read_file", "web_search"]
    router.total_calls.return_value = 0
    return router


# ── Project-specific fixtures ─────────────────────────────


@pytest.fixture
def valid_step():
    """Create a valid Step for testing."""
    from agent.planner.task_plan import Step, StepStatus
    return Step(
        step_id=1,
        description="读取配置文件",
        tool="read_file",
        tool_input={"path": "config.yaml"},
        depends_on=[],
        is_parallel=False,
        success_criteria="成功读取文件内容",
        fallback="手动指定路径",
        status=StepStatus.PENDING,
    )


@pytest.fixture
def valid_plan(valid_step):
    """Create a valid TaskPlan with 2 steps."""
    from agent.planner.task_plan import TaskPlan, Step
    step2 = Step(
        step_id=2,
        description="分析文件内容",
        tool="",
        depends_on=[1],
        is_parallel=False,
        success_criteria="得出分析结论",
        fallback="",
    )
    return TaskPlan(
        task_id="task_test_001",
        goal="测试目标",
        steps=[valid_step, step2],
        context={"retrieved_context": ""},
        estimated_tools=["read_file"],
    )


@pytest.fixture
def success_result():
    """A successful ToolResult."""
    from agent.tools.schema import ToolResult, ToolResultStatus
    return ToolResult(
        call_id="tc_001",
        tool="read_file",
        status=ToolResultStatus.SUCCESS,
        data={"content": "file content here", "path": "/test/file.py"},
    )


@pytest.fixture
def error_result():
    """A failed ToolResult."""
    from agent.tools.schema import ToolResult, ToolResultStatus
    return ToolResult(
        call_id="tc_002",
        tool="web_search",
        status=ToolResultStatus.ERROR,
        error="Connection timeout",
    )
