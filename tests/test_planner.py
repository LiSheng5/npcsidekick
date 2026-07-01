"""
测试 Planner — 任务分解与计划生成。
"""

import json
import pytest
from unittest.mock import MagicMock

from agent.planner.planner import Planner
from agent.planner.task_plan import TaskPlan, Step, StepStatus
from agent.llm.client import LLMClient


class TestPlanner:
    """Planner 核心功能测试。"""

    def test_plan_returns_task_plan(self, mock_llm_client, mock_memory_manager,
                                     mock_tool_registry):
        """plan() 应该返回一个 TaskPlan 对象。"""
        planner = Planner(mock_llm_client, mock_memory_manager, mock_tool_registry)

        # 配置 LLM 返回基本 plan
        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm_client.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_task_plan",
                arguments=json.dumps({
                    "goal": "测试任务",
                    "steps": [
                        {"step_id": 1, "description": "第一步", "success_criteria": "完成"},
                        {"step_id": 2, "description": "第二步", "success_criteria": "完成",
                         "depends_on": [1]},
                    ],
                }),
            ))],
        )

        plan = planner.plan("执行测试任务")

        assert isinstance(plan, TaskPlan)
        assert plan.goal == "测试任务"
        assert len(plan.steps) == 2

    def test_plan_empty_steps_generates_default(self, mock_llm_client,
                                                  mock_memory_manager, mock_tool_registry):
        """如果 LLM 返回 0 步，应该自动生成一个默认步骤。"""
        planner = Planner(mock_llm_client, mock_memory_manager, mock_tool_registry)

        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm_client.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_task_plan",
                arguments=json.dumps({"goal": "空任务", "steps": []}),
            ))],
        )

        plan = planner.plan("空任务")

        assert len(plan.steps) == 1
        assert "回复" in plan.steps[0].description

    def test_plan_injects_conversation_history(self, mock_llm_client,
                                                 mock_memory_manager, mock_tool_registry):
        """plan() 应该通过 plan.context 注入对话历史。"""
        mock_memory_manager.get_history_for_context.return_value = "[user] 你好\n[assistant] 你好！"

        planner = Planner(mock_llm_client, mock_memory_manager, mock_tool_registry)

        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm_client.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_task_plan",
                arguments=json.dumps({"goal": "测试", "steps": [
                    {"step_id": 1, "description": "测试步骤", "success_criteria": "OK"},
                ]}),
            ))],
        )

        plan = planner.plan("测试")

        assert "conversation_history" in plan.context
        assert "[user] 你好" in plan.context["conversation_history"]

    def test_plan_llm_error_fallback(self, mock_llm_client, mock_memory_manager,
                                       mock_tool_registry):
        """LLM 调用失败时应该回退到简单计划。"""
        mock_llm_client.chat.side_effect = Exception("API 不可用")

        planner = Planner(mock_llm_client, mock_memory_manager, mock_tool_registry)

        plan = planner.plan("测试")

        assert isinstance(plan, TaskPlan)
        assert len(plan.steps) >= 1  # fallback 至少有 1 步

    def test_replan_preserves_completed_steps(self, mock_llm_client,
                                                mock_memory_manager, mock_tool_registry,
                                                valid_plan, valid_step):
        """replan() 应该保留已完成的步骤。"""
        # 标记第一步成功
        valid_step.status = StepStatus.SUCCESS
        valid_step.result = {"data": "done"}

        planner = Planner(mock_llm_client, mock_memory_manager, mock_tool_registry)

        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm_client.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_task_plan",
                arguments=json.dumps({"goal": valid_plan.goal, "steps": [
                    {"step_id": 3, "description": "替代步骤", "success_criteria": "OK"},
                ]}),
            ))],
        )

        new_plan = planner.replan(valid_plan, valid_plan.steps[1], "模拟错误")

        assert new_plan.task_id == valid_plan.task_id
        # 应该包含已成功的 step 1 + 新替代步骤
        assert any(s.step_id == 1 for s in new_plan.steps)

    def test_build_task_plan_populates_context(self, mock_llm_client,
                                                 mock_memory_manager, mock_tool_registry):
        """_build_task_plan() 应该填充 context 字段。"""
        planner = Planner(mock_llm_client, mock_memory_manager, mock_tool_registry)

        plan = planner._build_task_plan(
            plan_data={"goal": "测试", "steps": [
                {"step_id": 1, "description": "步骤", "success_criteria": "OK",
                 "tool": "read_file"},
            ]},
            context_str="搜索上下文",
            conversation_history="对话历史",
        )

        assert plan.context["retrieved_context"] == "搜索上下文"
        assert plan.context["conversation_history"] == "对话历史"
        assert "read_file" in plan.estimated_tools

    def test_plan_with_dependencies(self, mock_llm_client, mock_memory_manager,
                                       mock_tool_registry):
        """plan() 应该正确处理步骤依赖关系。"""
        planner = Planner(mock_llm_client, mock_memory_manager, mock_tool_registry)

        from tests.conftest import FakeLLMResponse, FakeToolCall, FakeFunctionCall
        mock_llm_client.chat.return_value = FakeLLMResponse(
            tool_calls=[FakeToolCall(function=FakeFunctionCall(
                name="emit_task_plan",
                arguments=json.dumps({
                    "goal": "依赖任务",
                    "steps": [
                        {"step_id": 1, "description": "独立步骤", "success_criteria": "完成",
                         "depends_on": []},
                        {"step_id": 2, "description": "依赖步骤1", "success_criteria": "完成",
                         "depends_on": [1]},
                        {"step_id": 3, "description": "也依赖步骤1", "success_criteria": "完成",
                         "depends_on": [1]},
                    ],
                }),
            ))],
        )

        plan = planner.plan("依赖任务")

        assert plan.steps[1].depends_on == [1]
        assert plan.steps[2].depends_on == [1]
        assert plan.steps[0].depends_on == []
