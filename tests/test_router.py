"""
测试 ToolRouter — 工具分发与选择。
"""

import pytest
from unittest.mock import MagicMock, patch

from agent.tools.router import ToolRouter
from agent.tools.registry import ToolRegistry
from agent.tools.schema import (
    ToolProtocol, ToolCall, ToolResult, ToolResultStatus, ToolSchema
)


# ── Fake Tool ──────────────────────────────────────────────


class FakeReadTool(ToolProtocol):
    """Fake 文件读取工具。"""
    schema = ToolSchema(
        name="read_file",
        description="读取文件内容",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        tags=["file", "read"],
        category="file",
    )

    def validate(self, call: ToolCall):
        return True, ""

    def execute(self, call: ToolCall):
        return ToolResult(
            call_id=call.call_id,
            tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"content": "fake content", "path": call.input.get("path", "")},
        )


class FakeWriteTool(ToolProtocol):
    schema = ToolSchema(
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

    def validate(self, call: ToolCall):
        if "/forbidden/" in call.input.get("path", ""):
            return False, "路径被禁止"
        return True, ""

    def execute(self, call: ToolCall):
        return ToolResult(
            call_id=call.call_id,
            tool="write_file",
            status=ToolResultStatus.SUCCESS,
            data={"written": True},
        )


class FakeSearchTool(ToolProtocol):
    schema = ToolSchema(
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

    def validate(self, call: ToolCall):
        return True, ""

    def execute(self, call: ToolCall):
        return ToolResult(
            call_id=call.call_id,
            tool="web_search",
            status=ToolResultStatus.SUCCESS,
            data={"results": ["result1", "result2"]},
        )


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def real_registry():
    """创建包含真实 fake 工具的 registry。"""
    reg = ToolRegistry()
    reg.register(FakeReadTool())
    reg.register(FakeWriteTool())
    reg.register(FakeSearchTool())
    return reg


@pytest.fixture
def router(real_registry):
    return ToolRouter(real_registry)


# ── Tests ──────────────────────────────────────────────────


class TestRouterDispatch:
    """工具分发测试。"""

    def test_dispatch_valid_tool(self, router):
        """分发到有效工具应该成功。"""
        call = ToolCall(tool="read_file", input={"path": "/tmp/test.txt"})
        result = router.dispatch(call)

        assert result.ok is True
        assert result.tool == "read_file"
        assert "content" in result.data
        assert result.duration_ms >= 0

    def test_dispatch_unknown_tool(self, router):
        """分发到不存在的工具应返回错误。"""
        call = ToolCall(tool="non_existent")
        result = router.dispatch(call)

        assert result.ok is False
        assert result.status == ToolResultStatus.ERROR

    def test_dispatch_rejected_tool(self, router):
        """写被禁止路径 → REJECTED。"""
        call = ToolCall(tool="write_file", input={"path": "/forbidden/secret.txt", "content": "data"})
        result = router.dispatch(call)

        assert result.status == ToolResultStatus.REJECTED
        assert "禁止" in result.error

    def test_dispatch_assigns_call_id(self, router):
        """dispatch 应该自动分配 call_id。"""
        call = ToolCall(tool="read_file", input={"path": "/a.txt"})
        result = router.dispatch(call)

        assert result.call_id
        assert result.call_id.startswith("tc_")

    def test_dispatch_many(self, router):
        """dispatch_many 应该顺序分发所有 calls。"""
        calls = [
            ToolCall(tool="read_file", input={"path": "/a.txt"}),
            ToolCall(tool="web_search", input={"query": "test"}),
        ]
        results = router.dispatch_many(calls)

        assert len(results) == 2
        assert all(r.ok for r in results)

    def test_tool_execution_exception_caught(self, router):
        """工具抛出异常 → ERROR 状态。"""

        class BuggyTool(ToolProtocol):
            schema = ToolSchema(
                name="buggy",
                description="有 bug 的工具",
                parameters={"type": "object", "properties": {}},
                tags=["test"],
            )

            def validate(self, call):
                return True, ""

            def execute(self, call):
                raise RuntimeError("模拟崩溃")

        router.registry.register(BuggyTool())

        call = ToolCall(tool="buggy", input={})
        result = router.dispatch(call)

        assert result.status == ToolResultStatus.ERROR
        assert "RuntimeError" in result.error


class TestRouterRecommendation:
    """工具推荐测试。"""

    def test_recommend_tools_by_keyword(self, router):
        """关键词匹配推荐。"""
        schemas = router.recommend_tools("请帮我读取文件")

        names = [s.name for s in schemas]
        assert "read_file" in names

    def test_recommend_for_step_returns_names(self, router):
        """recommend_for_step 返回工具名称列表。"""
        names = router.recommend_for_step("搜索网页")

        assert "web_search" in names

    def test_call_history_tracking(self, router):
        """call_history 记录所有调用。"""
        router.dispatch(ToolCall(tool="read_file", input={"path": "/a.txt"}))
        router.dispatch(ToolCall(tool="web_search", input={"query": "test"}))

        assert router.total_calls() == 2
        assert len(router.call_history) == 2

    def test_last_result(self, router):
        """last_result() 返回最后一个结果。"""
        router.dispatch(ToolCall(tool="read_file", input={"path": "/a.txt"}))

        last = router.last_result()
        assert last is not None
        assert last.tool == "read_file"


# ── Fake Dangerous Tool（审批门控测试用）─────────────────


class FakeRunCodeTool(ToolProtocol):
    schema = ToolSchema(
        name="run_code",
        description="执行 Python 代码",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        tags=["code", "execute"],
        category="code",
        requires_approval=True,   # ← 需要审批
    )

    def validate(self, call: ToolCall):
        return True, ""

    def execute(self, call: ToolCall):
        return ToolResult(
            call_id=call.call_id,
            tool="run_code",
            status=ToolResultStatus.SUCCESS,
            data={"output": "ok"},
        )


# ── Approval Gate Tests ────────────────────────────────────


class TestRouterApproval:

    @pytest.fixture
    def registry_with_dangerous(self, real_registry):
        real_registry.register(FakeRunCodeTool())
        return real_registry

    def test_dangerous_tool_rejected_without_handler(self, registry_with_dangerous):
        """没有 approval_handler 时，requires_approval 工具必须 REJECT（安全默认）。"""
        router = ToolRouter(registry_with_dangerous)  # ← 故意不传 handler
        call = ToolCall(tool="run_code", input={"code": "print(1)"})
        result = router.dispatch(call)

        assert result.status == ToolResultStatus.REJECTED
        assert "审批" in result.error

    def test_dangerous_tool_approved_when_handler_returns_true(self, registry_with_dangerous):
        """handler 返回 True → 正常执行。"""
        router = ToolRouter(registry_with_dangerous, approval_handler=lambda c, s: True)
        call = ToolCall(tool="run_code", input={"code": "print(1)"})
        result = router.dispatch(call)

        assert result.ok is True
        assert result.data["output"] == "ok"

    def test_dangerous_tool_rejected_when_handler_returns_false(self, registry_with_dangerous):
        """handler 返回 False → REJECTED。"""
        router = ToolRouter(registry_with_dangerous, approval_handler=lambda c, s: False)
        call = ToolCall(tool="run_code", input={"code": "print(1)"})
        result = router.dispatch(call)

        assert result.status == ToolResultStatus.REJECTED
        assert "拒绝" in result.error

    def test_safe_tool_unaffected_by_approval_gate(self, registry_with_dangerous):
        """不需要审批的工具不应触发 handler。"""
        handler_called = [0]

        def counting_handler(c, s):
            handler_called[0] += 1
            return True

        router = ToolRouter(registry_with_dangerous, approval_handler=counting_handler)
        result = router.dispatch(ToolCall(tool="read_file", input={"path": "/a.txt"}))

        assert result.ok is True
        assert handler_called[0] == 0  # ← handler 根本不该被调用

    def test_approval_handler_receives_call_and_schema(self, registry_with_dangerous):
        """handler 应收到正确的 ToolCall 和 ToolSchema 参数。"""
        received = {}

        def capture_handler(call, schema):
            received["call"] = call
            received["schema"] = schema
            return True

        router = ToolRouter(registry_with_dangerous, approval_handler=capture_handler)
        call = ToolCall(tool="run_code", input={"code": "print(42)"}, reason="测试审批")
        router.dispatch(call)

        assert received["call"].tool == "run_code"
        assert received["call"].input == {"code": "print(42)"}
        assert received["schema"].name == "run_code"
        assert received["schema"].requires_approval is True
