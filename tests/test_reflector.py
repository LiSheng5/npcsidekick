"""
测试 Reflector — LLM 驱动的自我反思与决策。
"""

import json
import pytest
from unittest.mock import MagicMock

from agent.planner.reflector import (
    Reflector, ReflectionDecision, REFLECTION_TOOL, REFLECTION_PLAN_TOOL
)
from agent.planner.task_plan import Step, StepStatus, TaskPlan
from agent.tools.schema import ToolResult, ToolResultStatus


def make_step(step_id=1, description="测试步骤", success_criteria="",
              tool="read_file", retry_count=0):
    return Step(
        step_id=step_id,
        description=description,
        tool=tool,
        success_criteria=success_criteria,
        retry_count=retry_count,
    )


def make_plan(goal="测试", num_steps=3):
    steps = []
    for i in range(1, num_steps + 1):
        steps.append(Step(
            step_id=i,
            description=f"步骤 {i}",
            success_criteria="" if i < num_steps else "最终完成",
        ))
    return TaskPlan(
        task_id="test_001",
        goal=goal,
        steps=steps,
        context={},
    )


class TestReflectorFastPath:
    """快速规则路径测试 (不走 LLM)。"""

    def test_reflection_disabled_falls_back_to_rules(self, mocker):
        """REFLECTION_ENABLED=False 时直接使用规则。"""
        import config
        mocker.patch.object(config, 'REFLECTION_ENABLED', False)

        reflector = Reflector(llm=None)
        step = make_step()
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"content": "data"},
        )
        plan = make_plan()

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.CONTINUE

    def test_rejected_always_replan(self):
        """被拒绝 (安全检查) → REPLAN。"""
        reflector = Reflector(llm=None)
        step = make_step()
        result = ToolResult(
            call_id="tc_1", tool="write_file",
            status=ToolResultStatus.REJECTED,
            error="路径不在允许范围内",
        )
        plan = make_plan()

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.REPLAN

    def test_timeout_with_retries_left_retry(self, mocker):
        """超时 + 还有重试次数 → RETRY。"""
        import config
        mocker.patch.object(config, 'MAX_RETRIES', 3)

        reflector = Reflector(llm=None)
        step = make_step(retry_count=1)  # 还有 2 次机会
        result = ToolResult(
            call_id="tc_1", tool="web_search",
            status=ToolResultStatus.TIMEOUT,
            error="请求超时",
        )
        plan = make_plan()

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.RETRY

    def test_timeout_no_retries_left_replan(self, mocker):
        """超时 + 重试耗尽 → REPLAN。"""
        import config
        mocker.patch.object(config, 'MAX_RETRIES', 3)

        reflector = Reflector(llm=None)
        step = make_step(retry_count=3)  # 已重试 3 次
        result = ToolResult(
            call_id="tc_1", tool="web_search",
            status=ToolResultStatus.TIMEOUT,
        )
        plan = make_plan()

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.REPLAN

    def test_success_no_criteria_not_last_continue(self):
        """成功 + 无成功标准 + 非最后一步 → CONTINUE。"""
        reflector = Reflector(llm=None)
        step = make_step(step_id=1, success_criteria="")
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"ok": True},
        )
        plan = make_plan(num_steps=3)

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.CONTINUE

    def test_success_no_criteria_last_step_stop(self):
        """成功 + 无成功标准 + 最后一步 → STOP。"""
        reflector = Reflector(llm=None)
        plan = make_plan(num_steps=2)
        step = make_step(step_id=2, success_criteria="")
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"ok": True},
        )

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.STOP


class TestReflectorLLMPath:
    """LLM 路径测试 (使用 mock LLM)。"""

    def test_llm_reflection_success_continue(self, mocker):
        """LLM 返回 continue → CONTINUE。"""
        import config
        mocker.patch.object(config, 'REFLECTION_USE_LLM', True)

        mock_llm = MagicMock()
        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_reflection",
                arguments=json.dumps({
                    "decision": "continue",
                    "criteria_met": True,
                    "reason": "步骤已成功。",
                }),
            ))],
        )

        reflector = Reflector(llm=mock_llm)
        step = make_step(success_criteria="返回有效数据")
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"content": "data"},
        )
        plan = make_plan(num_steps=3)

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.CONTINUE

    def test_llm_reflection_error_retry(self, mocker):
        """LLM 对错误的步骤返回 retry。"""
        import config
        mocker.patch.object(config, 'REFLECTION_USE_LLM', True)

        mock_llm = MagicMock()
        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_reflection",
                arguments=json.dumps({
                    "decision": "retry",
                    "criteria_met": False,
                    "reason": "网络临时故障，重试可能成功。",
                    "suggestion": "等待 2 秒后重试",
                }),
            ))],
        )

        reflector = Reflector(llm=mock_llm)
        step = make_step(retry_count=0)
        result = ToolResult(
            call_id="tc_1", tool="web_search",
            status=ToolResultStatus.ERROR,
            error="Connection timeout",
        )
        plan = make_plan()

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.RETRY

    def test_llm_reflection_replan(self, mocker):
        """LLM 对根本性失败返回 replan。"""
        import config
        mocker.patch.object(config, 'REFLECTION_USE_LLM', True)

        mock_llm = MagicMock()
        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_reflection",
                arguments=json.dumps({
                    "decision": "replan",
                    "criteria_met": False,
                    "reason": "文件不存在，需要换一个工具。",
                }),
            ))],
        )

        reflector = Reflector(llm=mock_llm)
        step = make_step()
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.ERROR,
            error="File not found",
        )
        plan = make_plan()

        decision = reflector.reflect(step, result, plan)

        assert decision == ReflectionDecision.REPLAN

    def test_llm_unavailable_fallback(self, mocker):
        """LLM 调用失败 → 回退到规则判断。"""
        import config
        mocker.patch.object(config, 'REFLECTION_USE_LLM', True)

        mock_llm = MagicMock()
        mock_llm.chat.side_effect = Exception("API 不可用")

        reflector = Reflector(llm=mock_llm)
        step = make_step(retry_count=0)
        result = ToolResult(
            call_id="tc_1", tool="web_search",
            status=ToolResultStatus.ERROR,
            error="timeout",
        )
        plan = make_plan(num_steps=2)

        decision = reflector.reflect(step, result, plan)

        # 回退规则: 错误 + 有重试次数 → RETRY
        assert decision == ReflectionDecision.RETRY

    def test_use_llm_disabled_uses_fallback(self, mocker):
        """REFLECTION_USE_LLM=False 时不调用 LLM。"""
        import config
        mocker.patch.object(config, 'REFLECTION_USE_LLM', False)

        mock_llm = MagicMock()
        reflector = Reflector(llm=mock_llm)
        step = make_step(success_criteria="返回数据")
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"content": "data"},
        )
        plan = make_plan(num_steps=3)

        decision = reflector.reflect(step, result, plan)

        # 不应该调用 LLM
        mock_llm.chat.assert_not_called()


class TestReflectorPlanLevel:
    """计划级反思测试。"""

    def test_all_success_returns_stop(self):
        """全部成功 → STOP。"""
        reflector = Reflector(llm=None)
        plan = make_plan(num_steps=2)
        for s in plan.steps:
            s.status = StepStatus.SUCCESS

        decision = reflector.reflect_plan(plan, [])

        assert decision == ReflectionDecision.STOP
        # 注意: plan.is_success() 检查所有步骤成功

    def test_no_failed_returns_continue(self):
        """无失败步骤且有待执行步骤 → CONTINUE。"""
        reflector = Reflector(llm=None)
        plan = make_plan(num_steps=3)
        plan.steps[0].status = StepStatus.SUCCESS
        # step 2-3 still pending

        decision = reflector.reflect_plan(plan, [])

        # is_success() 返回 False (还有 PENDING)，failed_steps=[] → CONTINUE
        assert decision == ReflectionDecision.CONTINUE

    def test_high_failure_ratio_asks_user(self):
        """失败率 > 50% → ASK_USER。"""
        reflector = Reflector(llm=None)
        plan = make_plan(num_steps=3)
        failed = [plan.steps[0], plan.steps[1]]  # 66% 失败

        decision = reflector.reflect_plan(plan, failed)

        assert decision == ReflectionDecision.ASK_USER

    def test_low_failure_ratio_replan(self):
        """失败率 <= 50% → REPLAN。"""
        reflector = Reflector(llm=None)
        plan = make_plan(num_steps=3)
        failed = [plan.steps[0]]  # 33% 失败

        decision = reflector.reflect_plan(plan, failed)

        assert decision == ReflectionDecision.REPLAN

    def test_plan_with_llm(self, mocker):
        """LLM 全局评估。"""
        import config
        mocker.patch.object(config, 'REFLECTION_USE_LLM', True)

        mock_llm = MagicMock()
        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_plan_reflection",
                arguments=json.dumps({
                    "decision": "replan",
                    "reason": "失败步骤需要替换工具。",
                }),
            ))],
        )

        reflector = Reflector(llm=mock_llm)
        plan = make_plan(num_steps=3)
        failed = [plan.steps[0]]

        decision = reflector.reflect_plan(plan, failed)

        assert decision == ReflectionDecision.REPLAN


class TestReflectorUtils:
    """Reflector 工具函数测试。"""

    def test_check_criteria_empty_check(self):
        """非空检查: 空数据 → False。"""
        reflector = Reflector(llm=None)
        step = make_step(success_criteria="返回结果不为空")
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={},
        )

        # data={} 但没有任何非空值
        assert reflector._check_criteria(step, result) is False

    def test_check_criteria_with_content_ok(self):
        """非空检查: 有数据 → True。"""
        reflector = Reflector(llm=None)
        step = make_step(success_criteria="返回内容不为空")
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"content": "hello world"},
        )

        assert reflector._check_criteria(step, result) is True

    def test_format_result_truncates_long_data(self):
        """_format_result 应该截断长数据。"""
        reflector = Reflector(llm=None)
        result = ToolResult(
            call_id="tc_1", tool="read_file",
            status=ToolResultStatus.SUCCESS,
            data={"text": "x" * 2000},
        )

        formatted = reflector._format_result(result)

        assert len(formatted) <= 900  # 800 + 截断消息
        assert "截断" in formatted

    def test_last_step_id(self):
        """_last_step_id 返回最大 step_id。"""
        plan = make_plan(num_steps=3)
        # step_ids are 1, 2, 3
        assert Reflector._last_step_id(plan) == 3
