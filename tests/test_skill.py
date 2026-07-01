"""
测试 Skill — 组合工具基类。
"""

import pytest
from unittest.mock import MagicMock, call

from agent.tools.skill import Skill, AnalyzeCodeSkill
from agent.tools.schema import (
    ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus
)


# ── Fake Skill (for testing base class) ────────────────────


class FakeEchoSkill(Skill):
    """测试用 Skill: 将输入分解为 2 个 echo 子调用。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="echo_skill",
            description="测试 skill — echo 输入",
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "要 echo 的文本"},
                },
                "required": ["task"],
            },
            category="test",
            tags=["test", "skill"],
        )

    def decompose(self, task: str, call: ToolCall) -> list:
        return [
            ToolCall(tool="echo", input={"text": f"part1: {task}"}, reason="first half"),
            ToolCall(tool="echo", input={"text": f"part2: {task}"}, reason="second half"),
        ]


class FakeFailingSkill(Skill):
    """测试用 Skill: 分解到一个会失败的工具。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="failing_skill",
            description="测试 skill — 部分失败",
            parameters={"type": "object", "properties": {}},
            category="test",
            tags=["test", "skill"],
        )

    def decompose(self, task: str, call: ToolCall) -> list:
        return [
            ToolCall(tool="always_fail", input={}, reason="这步会失败"),
        ]


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def mock_dispatcher():
    """Mock dispatcher that returns success for 'echo', error for 'always_fail'."""
    def dispatch(call: ToolCall) -> ToolResult:
        if call.tool == "echo":
            return ToolResult(
                call_id=call.call_id or "tc_echo",
                tool="echo",
                status=ToolResultStatus.SUCCESS,
                data={"echo": call.input.get("text", "")},
            )
        if call.tool == "always_fail":
            return ToolResult(
                call_id=call.call_id or "tc_fail",
                tool="always_fail",
                status=ToolResultStatus.ERROR,
                error="模拟失败",
            )
        return ToolResult(
            call_id=call.call_id or "tc_unknown",
            tool=call.tool,
            status=ToolResultStatus.ERROR,
            error=f"未知工具: {call.tool}",
        )
    return dispatch


# ── Tests ──────────────────────────────────────────────────


class TestSkillBase:
    """Skill 基类功能测试。"""

    def test_skill_is_tool_protocol(self):
        """Skill 是 ToolProtocol 的子类。"""
        skill = FakeEchoSkill()
        assert isinstance(skill, ToolProtocol)

    def test_skill_has_name(self):
        """Skill 的 name 属性来自 schema.name。"""
        skill = FakeEchoSkill()
        assert skill.name == "echo_skill"

    def test_execute_without_dispatcher_raises(self):
        """未注入 dispatcher 时调用 execute 应抛出异常。"""
        skill = FakeEchoSkill()
        call = ToolCall(tool="echo_skill", input={"task": "hello"})

        with pytest.raises(RuntimeError, match="dispatcher"):
            skill.execute(call)

    def test_execute_decomposes_and_dispatches(self, mock_dispatcher):
        """execute 应该: decompose → dispatch 每个子调用 → synthesize。"""
        skill = FakeEchoSkill()
        skill.set_dispatcher(mock_dispatcher)

        call = ToolCall(tool="echo_skill", input={"task": "hello"})
        result = skill.execute(call)

        assert result.ok is True
        assert result.tool == "echo_skill"
        # 应该有 2 个子结果
        sub_results = result.data.get("sub_results", [])
        assert len(sub_results) == 2
        assert sub_results[0]["tool"] == "echo"
        assert sub_results[1]["tool"] == "echo"

    def test_execute_handles_sub_call_failure(self, mock_dispatcher):
        """子调用失败时 synthesize 应返回 ERROR。"""
        skill = FakeFailingSkill()
        skill.set_dispatcher(mock_dispatcher)

        call = ToolCall(tool="failing_skill", input={})
        result = skill.execute(call)

        assert result.ok is False
        assert result.status == ToolResultStatus.ERROR
        assert "模拟失败" in result.error

    def test_synthesize_with_no_results(self):
        """无子结果时返回空摘要。"""
        skill = FakeEchoSkill()
        result = skill.synthesize([], ToolCall(tool="echo_skill"))

        assert result.ok is True
        assert result.data["summary"] == "无子任务"

    def test_set_dispatcher(self, mock_dispatcher):
        """set_dispatcher 存储调度函数。"""
        skill = FakeEchoSkill()
        assert skill._dispatcher is None

        skill.set_dispatcher(mock_dispatcher)
        assert skill._dispatcher is not None

    def test_execute_exception_in_sub_call(self):
        """子调度抛出异常时被捕获为 ERROR。"""
        skill = FakeEchoSkill()

        def exploding_dispatcher(call):
            raise RuntimeError("Boom!")

        skill.set_dispatcher(exploding_dispatcher)

        call = ToolCall(tool="echo_skill", input={"task": "test"})
        result = skill.execute(call)

        assert result.ok is False
        assert "Boom" in result.error
        # 两个子调用都应该失败
        assert result.data["failure_count"] == 2


class TestAnalyzeCodeSkill:
    """AnalyzeCodeSkill 测试。"""

    def test_schema_is_valid(self):
        """schema 应该正确定义。"""
        skill = AnalyzeCodeSkill()
        schema = skill.schema

        assert schema.name == "analyze_code"
        assert schema.category == "code"
        assert "path" in schema.parameters.get("required", [])
        assert schema.is_readonly is True

    def test_decompose_creates_sub_calls(self):
        """decompose 应该生成 read_file + lint_code。"""
        skill = AnalyzeCodeSkill()
        call = ToolCall(tool="analyze_code", input={"path": "/test/main.py", "task": "分析"})

        sub_calls = skill.decompose("分析", call)

        assert len(sub_calls) == 2
        assert sub_calls[0].tool == "read_file"
        assert sub_calls[1].tool == "lint_code"
        assert sub_calls[0].input["path"] == "/test/main.py"

    def test_synthesize_merges_file_and_lint(self, mock_dispatcher):
        """synthesize 应该合并文件内容和 lint 结果。"""
        skill = AnalyzeCodeSkill()
        skill.set_dispatcher(mock_dispatcher)

        call = ToolCall(tool="analyze_code", input={"path": "/test/main.py"})
        result = skill.execute(call)

        assert result.ok is False  # 因为 mock_dispatcher 不认识 'lint_code'
        assert "analysis" not in result.data  # 因为 sub_results 有失败

    def test_end_to_end_with_custom_dispatcher(self):
        """端到端: 使用自定义 dispatcher 模拟 read_file + lint_code 成功。"""
        skill = AnalyzeCodeSkill()

        def custom_dispatch(call: ToolCall) -> ToolResult:
            if call.tool == "read_file":
                return ToolResult(
                    call_id=call.call_id, tool="read_file",
                    status=ToolResultStatus.SUCCESS,
                    data={"content": "print('hello')\n", "path": call.input["path"]},
                )
            if call.tool == "lint_code":
                return ToolResult(
                    call_id=call.call_id, tool="lint_code",
                    status=ToolResultStatus.SUCCESS,
                    data={"issues": []},
                )
            return ToolResult(
                call_id=call.call_id, tool=call.tool,
                status=ToolResultStatus.ERROR, error="unknown",
            )

        skill.set_dispatcher(custom_dispatch)

        call = ToolCall(tool="analyze_code", input={"path": "hello.py"})
        result = skill.execute(call)

        assert result.ok is True
        analysis = result.data.get("analysis", {})
        assert analysis.get("verdict") == "pass"
        assert analysis.get("lint_issues_count") == 0
        assert analysis.get("file_size_chars") == 15  # len("print('hello')\n") == 15
