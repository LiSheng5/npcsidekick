"""
Tests for agent.tools.schema — ToolCall, ToolResult, ToolSchema, ToolProtocol.
"""
import json

import pytest

from agent.tools.schema import (
    ToolCall,
    ToolResult,
    ToolResultStatus,
    ToolSchema,
    ToolProtocol,
)


# ═══════════════════════════════════════════════════════
# ToolResultStatus
# ═══════════════════════════════════════════════════════

class TestToolResultStatus:
    def test_values(self):
        assert ToolResultStatus.SUCCESS == "success"
        assert ToolResultStatus.ERROR == "error"
        assert ToolResultStatus.TIMEOUT == "timeout"
        assert ToolResultStatus.REJECTED == "rejected"


# ═══════════════════════════════════════════════════════
# ToolCall
# ═══════════════════════════════════════════════════════

class TestToolCall:
    def test_default_values(self):
        call = ToolCall(tool="read_file")
        assert call.tool == "read_file"
        assert call.input == {}
        assert call.reason == ""
        assert call.call_id == ""
        assert call.priority == 0
        assert call.depends_on == []
        assert call.timeout_ms == 60_000

    def test_full_construction(self):
        call = ToolCall(
            tool="write_file",
            input={"path": "/tmp/test.txt", "content": "hello"},
            reason="需要写入测试文件",
            call_id="tc_001",
            priority=3,
            depends_on=["tc_000"],
            timeout_ms=30_000,
        )
        assert call.tool == "write_file"
        assert call.input["path"] == "/tmp/test.txt"
        assert call.reason == "需要写入测试文件"
        assert call.call_id == "tc_001"
        assert call.priority == 3
        assert call.depends_on == ["tc_000"]
        assert call.timeout_ms == 30_000

    def test_to_json(self):
        call = ToolCall(tool="get_time", reason="查询时间")
        json_str = call.to_json()
        data = json.loads(json_str)
        assert data["tool"] == "get_time"
        assert data["reason"] == "查询时间"

    def test_from_json_with_dict(self):
        data = {"tool": "search", "input": {"q": "test"}, "reason": "搜索"}
        call = ToolCall.from_json(data)
        assert call.tool == "search"
        assert call.input == {"q": "test"}
        assert call.reason == "搜索"

    def test_from_json_with_string(self):
        json_str = '{"tool": "read_file", "input": {"path": "/x.py"}}'
        call = ToolCall.from_json(json_str)
        assert call.tool == "read_file"
        assert call.input == {"path": "/x.py"}

    def test_from_json_missing_keys(self):
        call = ToolCall.from_json({})
        assert call.tool == ""
        assert call.input == {}
        assert call.timeout_ms == 60_000  # default

    def test_from_openai_tool_call_valid(self):
        class FakeFunction:
            name = "search_web"
            arguments = '{"query": "Python 3.12 release notes"}'

        class FakeOpenAICall:
            id = "call_abc123"
            function = FakeFunction()

        call = ToolCall.from_openai_tool_call(FakeOpenAICall())
        assert call.tool == "search_web"
        assert call.input == {"query": "Python 3.12 release notes"}
        assert call.call_id == "call_abc123"

    def test_from_openai_tool_call_invalid_json_arguments(self):
        class FakeFunction:
            name = "bad_tool"
            arguments = "not-valid-json"

        class FakeOpenAICall:
            id = "call_bad"
            function = FakeFunction()

        call = ToolCall.from_openai_tool_call(FakeOpenAICall())
        assert call.tool == "bad_tool"
        assert call.input == {}  # falls back to empty dict

    def test_from_openai_tool_call_none_arguments(self):
        class FakeFunction:
            name = "tool"
            arguments = None

        class FakeOpenAICall:
            id = "call_none"
            function = FakeFunction()

        call = ToolCall.from_openai_tool_call(FakeOpenAICall())
        assert call.tool == "tool"
        assert call.input == {}

    def test_from_openai_tool_call_missing_id(self):
        class FakeFunction:
            name = "tool"
            arguments = '{}'

        class FakeOpenAICall:
            function = FakeFunction()
            # no id attribute

        call = ToolCall.from_openai_tool_call(FakeOpenAICall())
        assert call.call_id == ""


# ═══════════════════════════════════════════════════════
# ToolResult
# ═══════════════════════════════════════════════════════

class TestToolResult:
    def test_ok_for_success(self):
        result = ToolResult(call_id="c1", tool="t", status=ToolResultStatus.SUCCESS)
        assert result.ok is True

    def test_ok_for_non_success(self):
        for status in (ToolResultStatus.ERROR, ToolResultStatus.TIMEOUT, ToolResultStatus.REJECTED):
            result = ToolResult(call_id="c1", tool="t", status=status)
            assert result.ok is False, f"status {status} should not be ok"

    def test_default_values(self):
        result = ToolResult(call_id="c1", tool="t", status=ToolResultStatus.SUCCESS)
        assert result.data is None
        assert result.error is None
        assert result.duration_ms == 0.0

    def test_with_data(self):
        result = ToolResult(call_id="c1", tool="read_file", status=ToolResultStatus.SUCCESS,
                            data={"content": "line1\nline2", "lines": 2})
        assert result.data["content"] == "line1\nline2"

    def test_with_error(self):
        result = ToolResult(call_id="c1", tool="web_search", status=ToolResultStatus.ERROR,
                            error="Connection timeout")
        assert result.error == "Connection timeout"

    def test_to_dict(self):
        result = ToolResult(call_id="c1", tool="get_time", status=ToolResultStatus.SUCCESS,
                            data={"time": "12:00"}, duration_ms=150.5)
        d = result.to_dict()
        assert d["call_id"] == "c1"
        assert d["tool"] == "get_time"
        assert d["status"] == "success"
        assert d["data"] == {"time": "12:00"}
        assert d["duration_ms"] == 150.5


# ═══════════════════════════════════════════════════════
# ToolSchema
# ═══════════════════════════════════════════════════════

class TestToolSchema:
    def test_default_values(self):
        schema = ToolSchema(name="test", description="测试工具")
        assert schema.category == "general"
        assert schema.tags == []
        assert schema.requires_approval is False
        assert schema.allowed_intents == []
        assert schema.is_idempotent is True
        assert schema.is_readonly is True
        assert schema.estimated_duration_ms == 1000

    def test_to_openai_function_basic(self):
        schema = ToolSchema(
            name="get_time",
            description="获取当前时间",
            parameters={
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
                "required": [],
            },
        )
        func = schema.to_openai_function()
        assert func["type"] == "function"
        assert func["function"]["name"] == "get_time"
        assert func["function"]["description"] == "获取当前时间"
        assert "timezone" in func["function"]["parameters"]["properties"]

    def test_to_openai_function_empty_parameters(self):
        schema = ToolSchema(name="ping", description="Ping test")
        func = schema.to_openai_function()
        assert func["function"]["parameters"]["properties"] == {}
        assert func["function"]["parameters"]["required"] == []

    def test_to_dict(self):
        schema = ToolSchema(name="run_code", description="执行代码", category="code",
                            tags=["code", "dangerous"], is_readonly=False)
        d = schema.to_dict()
        assert d["name"] == "run_code"
        assert d["category"] == "code"
        assert d["is_readonly"] is False


# ═══════════════════════════════════════════════════════
# ToolProtocol
# ═══════════════════════════════════════════════════════

class TestToolProtocol:
    def test_schema_raises_not_implemented(self):
        class BadTool(ToolProtocol):
            pass
        tool = BadTool()
        with pytest.raises(NotImplementedError):
            _ = tool.schema

    def test_execute_raises_not_implemented(self):
        class BadTool(ToolProtocol):
            pass
        tool = BadTool()
        with pytest.raises(NotImplementedError):
            tool.execute(ToolCall(tool="bad"))

    def test_validate_default_passes(self):
        class SimpleTool(ToolProtocol):
            @property
            def schema(self):
                return ToolSchema(name="simple", description="simple")

        tool = SimpleTool()
        ok, msg = tool.validate(ToolCall(tool="simple"))
        assert ok is True
        assert msg == "ok"

    def test_name_delegates_to_schema(self):
        class NamedTool(ToolProtocol):
            @property
            def schema(self):
                return ToolSchema(name="my_tool", description="named")

        tool = NamedTool()
        assert tool.name == "my_tool"

    def test_repr(self):
        class ReprTool(ToolProtocol):
            @property
            def schema(self):
                return ToolSchema(name="repr_tool", description="repr test")

        tool = ReprTool()
        assert repr(tool) == "<Tool:repr_tool>"
