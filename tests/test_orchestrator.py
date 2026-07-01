"""
测试 AgentOrchestrator — 主编排器完整流程。
"""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from agent.orchestrator import AgentOrchestrator


# ── Module-level fixtures ──────────────────────────────────


@pytest.fixture
def setup_orch():
    """创建一个已初始化的 orchestrator，全部使用 mock。"""
    orch = AgentOrchestrator()

    # Mock 所有核心组件
    orch.llm = MagicMock()
    orch.memory = MagicMock()
    orch.registry = MagicMock()
    orch.router = MagicMock()
    orch.adapter = MagicMock()
    orch.planner = MagicMock()
    orch.executor = MagicMock()
    orch.reflector = MagicMock()

    # 设置默认返回值
    orch.memory.retrieve_for_planning.return_value = "上下文"
    orch.memory.get_history_for_context.return_value = "[user] 测试\n[assistant] 回复"
    orch.memory.maybe_compress.return_value = 0
    orch.registry.to_tool_descriptions.return_value = "- tool1: 工具1\n- tool2: 工具2"
    orch.adapter.to_openai_tools.return_value = []

    return orch


# ── Tests ──────────────────────────────────────────────────


class TestOrchestratorInitialization:
    """Orchestrator 初始化测试。"""

    def test_init_creates_empty_components(self):
        """初始化时所有组件应为 None。"""
        orch = AgentOrchestrator()

        assert orch.llm is None
        assert orch.memory is None
        assert orch.planner is None
        assert orch.executor is None
        assert orch.reflector is None

    def test_initialize_populates_components(self, mocker):
        """initialize() 应填充所有组件。"""
        mocker.patch('agent.orchestrator.LLMClient')
        mocker.patch('agent.orchestrator.AgentOrchestrator._register_builtin_tools')

        orch = AgentOrchestrator()
        orch.initialize()

        assert orch.llm is not None
        assert orch.memory is not None
        assert orch.planner is not None
        assert orch.executor is not None
        assert orch.reflector is not None

    def test_run_without_initialize_raises(self):
        """未初始化时调用 run() 应抛出 RuntimeError。"""
        orch = AgentOrchestrator()

        with pytest.raises(RuntimeError, match="未初始化"):
            orch.run("测试")


class TestOrchestratorRun:
    """run() 完整流程测试。"""

    def test_run_single_step_success(self, setup_orch):
        """单步成功 → 完整流程。"""
        orch = setup_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        plan = TaskPlan(
            task_id="task_test",
            goal="现在几点？",
            steps=[
                Step(step_id=1, description="查询时间", tool="get_time",
                     status=StepStatus.PENDING,  # NOT pre-completed
                     success_criteria="返回当前时间"),
            ],
            context={"conversation_history": ""},
        )
        orch.planner.plan.return_value = plan

        # Mock Executor: 修改 step 为成功 (模拟 execute_step 的就地修改)
        from agent.tools.schema import ToolResult, ToolResultStatus

        def execute_step_mock(step, plan, prev_results):
            step.status = StepStatus.SUCCESS
            step.result = {"time": "2025-01-15 10:30:00"}
            step.duration_ms = 50.0
            return step, ToolResult(call_id="tc_1", tool="get_time",
                                    status=ToolResultStatus.SUCCESS,
                                    data={"time": "2025-01-15 10:30:00"})

        orch.executor.execute_step.side_effect = execute_step_mock

        # Mock Reflector: 返回 CONTINUE (orchestrator 内部处理流程)
        from agent.planner.reflector import ReflectionDecision
        orch.reflector.reflect.return_value = ReflectionDecision.CONTINUE

        # Mock _synthesize 让 run 完成
        orch._synthesize = MagicMock(return_value="现在是 10:30。")

        answer = orch.run("现在几点？")

        orch.planner.plan.assert_called_once_with("现在几点？")
        orch.executor.execute_step.assert_called_once()
        orch.memory.add_message.assert_any_call("user", "现在几点？")
        orch.memory.save.assert_called()
        assert "10:30" in answer or "现在" in answer

    def test_run_with_retry(self, setup_orch):
        """步骤先失败(重试)然后成功。"""
        orch = setup_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        from agent.tools.schema import ToolResult, ToolResultStatus
        from agent.planner.reflector import ReflectionDecision

        plan = TaskPlan(
            task_id="task_retry",
            goal="搜索信息",
            steps=[
                Step(step_id=1, description="搜索", tool="web_search",
                     status=StepStatus.PENDING,
                     success_criteria="返回搜索结果"),
            ],
            context={"conversation_history": ""},
        )
        orch.planner.plan.return_value = plan

        # 使用 side_effect: 第一次失败, 第二次成功 (就地修改)
        from agent.tools.schema import ToolResult, ToolResultStatus
        call_count = [0]

        def execute_step_mock(step, plan, prev_results):
            call_count[0] += 1
            if call_count[0] == 1:
                step.status = StepStatus.FAILED
                step.error = "timeout"
                return step, ToolResult(call_id="tc_1", tool="web_search",
                                        status=ToolResultStatus.ERROR, error="timeout")
            step.status = StepStatus.SUCCESS
            step.result = {"results": ["r1", "r2"]}
            return step, ToolResult(call_id="tc_2", tool="web_search",
                                    status=ToolResultStatus.SUCCESS,
                                    data={"results": ["r1", "r2"]})

        orch.executor.execute_step.side_effect = execute_step_mock

        # Reflector: 前 2 次 RETRY, 然后 CONTINUE
        refl_count = [0]

        def reflect_mock(step, result, plan):
            refl_count[0] += 1
            if refl_count[0] == 1:
                return ReflectionDecision.RETRY
            return ReflectionDecision.CONTINUE

        orch.reflector.reflect.side_effect = reflect_mock

        orch._synthesize = MagicMock(return_value="搜索完成。")

        answer = orch.run("搜索信息")

        # 验证反射被调用了多次
        assert orch.reflector.reflect.call_count >= 2

    def test_run_replan(self, setup_orch):
        """步骤失败触发 replan。"""
        orch = setup_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        from agent.tools.schema import ToolResult, ToolResultStatus
        from agent.planner.reflector import ReflectionDecision

        plan = TaskPlan(
            task_id="task_replan",
            goal="读取配置",
            steps=[
                Step(step_id=1, description="读取", tool="read_file",
                     status=StepStatus.PENDING,
                     success_criteria="返回内容"),
            ],
            context={},
        )
        orch.planner.plan.return_value = plan

        def execute_step_mock(step, plan, prev_results):
            step.status = StepStatus.FAILED
            step.error = "文件不存在"
            return step, ToolResult(call_id="tc_1", tool="read_file",
                                    status=ToolResultStatus.ERROR, error="文件不存在")

        orch.executor.execute_step.side_effect = execute_step_mock
        orch.reflector.reflect.return_value = ReflectionDecision.REPLAN

        # Replan 返回新计划 (所有步骤成功 → is_complete = True)
        replan = TaskPlan(
            task_id="task_replan",
            goal="读取配置",
            steps=[
                Step(step_id=2, description="搜索配置", tool="web_search",
                     status=StepStatus.SUCCESS, result={"found": True}),
            ],
            context={},
        )
        orch.planner.replan.return_value = replan

        orch._synthesize = MagicMock(return_value="配置读取完成。")

        answer = orch.run("读取配置")

        orch.planner.replan.assert_called_once()

    def test_get_execution_summary_no_plan(self):
        """无活动计划时返回状态。"""
        orch = AgentOrchestrator()
        summary = orch.get_execution_summary()

        assert summary["status"] == "无活动计划"

    def test_get_memory_summary(self, setup_orch):
        orch = setup_orch
        orch.memory.summarize.return_value = "测试摘要"

        result = orch.get_memory_summary()
        assert result == "测试摘要"


class TestOrchestratorChat:
    """Chat 模式测试。"""

    def test_chat_greetings_short_circuit(self, mocker):
        """问候语应该走快速通道。"""
        orch = AgentOrchestrator()
        orch.memory = MagicMock()

        mocker.patch.object(orch, 'run')

        answer = orch.run_chat("你好")

        orch.run.assert_not_called()
        assert "帮" in answer or "你好" in answer or "什么" in answer

    def test_chat_non_greeting_calls_run(self, mocker):
        """非问候语走正常 run() 流程。"""
        orch = AgentOrchestrator()
        orch.memory = MagicMock()

        mocker.patch.object(orch, 'run', return_value="任务完成")

        answer = orch.run_chat("帮我修复 bug")

        orch.run.assert_called_once_with("帮我修复 bug")


class TestOrchestratorSynthesize:
    """_synthesize 测试。"""

    def test_override_answer(self, setup_orch):
        orch = setup_orch
        result = orch._synthesize(MagicMock(), "直接覆盖答案")
        assert result == "直接覆盖答案"

    def test_single_step_extracts_response_field(self, setup_orch):
        """单步成功 → 提取 response 字段。"""
        from agent.planner.task_plan import TaskPlan, Step, StepStatus

        step = Step(step_id=1, description="搜索", status=StepStatus.SUCCESS,
                    result={"response": "搜索完成，找到 3 个结果"})
        plan = TaskPlan(task_id="t1", goal="搜索", steps=[step], context={})

        orch = setup_orch
        answer = orch._synthesize(plan)

        assert "搜索完成" in answer

    def test_single_step_extracts_time_field(self, setup_orch):
        """时间工具结果 → 自然语言格式化。"""
        from agent.planner.task_plan import TaskPlan, Step, StepStatus

        step = Step(step_id=1, description="查询时间", status=StepStatus.SUCCESS,
                    result={"time": "2025-06-15 14:30:00"})
        plan = TaskPlan(task_id="t1", goal="几点", steps=[step], context={})

        orch = setup_orch
        answer = orch._synthesize(plan)

        assert "2025" in answer

    def test_multi_step_with_failed(self, setup_orch):
        """多步骤 + 失败步骤的场景。"""
        from agent.planner.task_plan import TaskPlan, Step, StepStatus

        step1 = Step(step_id=1, description="读文件", status=StepStatus.SUCCESS,
                     result={"content": "file data"})
        step2 = Step(step_id=2, description="分析", status=StepStatus.FAILED,
                     error="解析失败")
        plan = TaskPlan(task_id="t1", goal="分析文件", steps=[step1, step2], context={})

        orch = setup_orch
        # Mock LLM 合成
        orch.llm.chat.return_value = MagicMock(content="分析完成，但第二步失败。")

        answer = orch._synthesize(plan)

        assert len(answer) > 0
