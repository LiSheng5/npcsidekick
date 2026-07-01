"""
测试 Executor — 步骤执行与 LLM 工具选择。
"""

import pytest
from unittest.mock import MagicMock, patch

from agent.executor.executor import Executor, EXECUTOR_SYSTEM_PROMPT
from agent.planner.task_plan import Step, StepStatus, TaskPlan
from agent.tools.schema import ToolResult, ToolResultStatus, ToolCall
from agent.tools.router import ToolRouter
from agent.tools.registry import ToolRegistry


def make_step(step_id=1, description="执行测试步骤", tool="", tool_input=None,
              success_criteria="完成执行", retry_count=0, depends_on=None):
    return Step(
        step_id=step_id,
        description=description,
        tool=tool,
        tool_input=tool_input,
        depends_on=depends_on or [],
        success_criteria=success_criteria,
        retry_count=retry_count,
    )


def make_plan(goal="测试目标", context=None):
    steps = [make_step(1)]
    return TaskPlan(
        task_id="test_exec_001",
        goal=goal,
        steps=steps,
        context=context or {},
    )


class TestExecutorBasic:
    """Executor 基本功能测试。"""

    def test_execute_step_success(self, mock_llm_client, mock_tool_router,
                                    mock_tool_registry):
        """执行一个成功的步骤。"""
        executor = Executor(mock_llm_client, mock_tool_router, mock_tool_registry)
        step = make_step()
        plan = make_plan()

        updated_step, result = executor.execute_step(step, plan, {})

        assert updated_step.status == StepStatus.SUCCESS
        assert updated_step.result is not None
        assert result.ok is True

    def test_execute_step_with_recommended_tool_direct_dispatch(self, mock_llm_client,
                                                                  mock_tool_router,
                                                                  mock_tool_registry):
        """指定工具+输入 → 直接分发，不调用 LLM。"""
        mock_llm_client.chat = MagicMock()

        executor = Executor(mock_llm_client, mock_tool_router, mock_tool_registry)
        step = make_step(tool="read_file", tool_input={"path": "/test.txt"})
        plan = make_plan()

        updated_step, result = executor.execute_step(step, plan, {})

        assert updated_step.status == StepStatus.SUCCESS
        # 不应该呼叫 LLM — 直接分发给工具
        mock_llm_client.chat.assert_not_called()

    def test_execute_step_llm_fallback_on_empty_tool(self, mock_llm_client,
                                                       mock_tool_router,
                                                       mock_tool_registry):
        """没有指定工具 → 调用 LLM 选择。"""
        from tests.conftest import FakeLLMResponse
        mock_llm_client.chat.return_value = FakeLLMResponse(
            content="我来处理这个任务。",
            tool_calls=None,
        )

        executor = Executor(mock_llm_client, mock_tool_router, mock_tool_registry)
        step = make_step(tool="")
        plan = make_plan()

        updated_step, result = executor.execute_step(step, plan, {})

        assert result.ok is True
        assert result.tool == "direct_response"

    def test_execute_step_injects_conversation_history(self, mock_llm_client,
                                                         mock_tool_router,
                                                         mock_tool_registry):
        """Executor 应该将对话历史注入 LLM 上下文。"""
        from tests.conftest import FakeLLMResponse
        mock_llm_client.chat.return_value = FakeLLMResponse(
            content="直接回复",
            tool_calls=None,
        )

        executor = Executor(mock_llm_client, mock_tool_router, mock_tool_registry)
        step = make_step(tool="")  # 无工具 → 走 LLM
        plan = make_plan(context={
            "conversation_history": "[user] 帮我找 bug\n[assistant] 好的",
        })

        executor.execute_step(step, plan, {})

        # 验证 LLM 被调用时包含对话历史
        call_args = mock_llm_client.chat.call_args
        messages = call_args[0][0]
        # system prompt 包含 context
        system_msg = messages[0]["content"]
        assert "对话历史" in system_msg

    def test_execute_pending_steps(self, mock_llm_client, mock_tool_router,
                                     mock_tool_registry):
        """execute_pending_steps 应该执行所有就绪步骤。"""
        executor = Executor(mock_llm_client, mock_tool_router, mock_tool_registry)

        plan = TaskPlan(
            task_id="test_multi",
            goal="多步任务",
            steps=[
                make_step(1),
                make_step(2, depends_on=[1]),
            ],
            context={},
        )
        plan.steps[0].status = StepStatus.SUCCESS  # step 1 已完成
        plan.steps[0].result = {"done": True}

        results = executor.execute_pending_steps(plan)

        assert len(results) >= 1  # step 2 就绪

    def test_execute_step_retry_on_failure(self, mock_llm_client, mock_tool_router,
                                             mock_tool_registry):
        """失败步骤应该触发重试。"""
        # 前 2 次失败，第 3 次成功
        mock_tool_router.dispatch.side_effect = [
            MagicMock(ok=False, error="临时错误", status="error",
                      call_id="tc_1", tool="test"),
            MagicMock(ok=False, error="再次失败", status="error",
                      call_id="tc_2", tool="test"),
            MagicMock(ok=True, data={"result": "success!"}, status="success",
                      call_id="tc_3", tool="test", error=None),
        ]

        # 需要让 executor 走 LLM 路径 (不直接分发)
        executor = Executor(mock_llm_client, mock_tool_router, mock_tool_registry)

        # 返回带 tool call 的 LLM 响应让它走 dispatch
        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        import json
        mock_llm_client.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="read_file",
                arguments=json.dumps({"path": "/test.txt"}),
            ))],
        )

        step = make_step(retry_count=0)
        plan = make_plan()

        updated_step, result = executor.execute_step(step, plan, {})

        # 应该重试到成功
        assert updated_step.status == StepStatus.SUCCESS
        assert updated_step.retry_count >= 1  # 至少重试了 1 次

    def test_needs_llm_decision_with_keywords(self):
        """包含决策关键词的步骤应该走 LLM。"""
        executor = Executor(MagicMock(), MagicMock(), MagicMock())

        step = make_step(description="分析代码并判断错误")
        assert executor._needs_llm_decision(step) is True

        step2 = make_step(description="读取文件")
        step2.tool = "read_file"
        assert executor._needs_llm_decision(step2) is False

    def test_needs_llm_decision_no_tool(self):
        """没有指定工具 → 需要 LLM。"""
        executor = Executor(MagicMock(), MagicMock(), MagicMock())
        step = make_step(tool="")

        assert executor._needs_llm_decision(step) is True
