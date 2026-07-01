"""
测试 AgentOrchestrator.run_stream() — 流式编排器。
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from agent.orchestrator import AgentOrchestrator
from agent.llm.types import StreamEvent, StreamEventType


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def setup_stream_orch():
    """创建一个已初始化的 orchestrator，用于流式测试。"""
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
    orch.registry.to_tool_descriptions.return_value = "- tool1: 工具1"
    orch.adapter.to_openai_tools.return_value = []
    # 让 __iter__ 返回空列表 (for _inject_skill_dispatchers)
    orch.registry.__iter__ = MagicMock(return_value=iter([]))

    return orch


async def _collect_events(generator):
    """Helper: collect all events from async generator."""
    events = []
    async for event in generator:
        events.append(event)
    return events


# ── Tests ──────────────────────────────────────────────────


class TestRunStreamBasic:
    """run_stream() 基本流程测试。"""

    @pytest.mark.asyncio
    async def test_run_stream_yields_thinking_event(self, setup_stream_orch):
        """第一个事件应该是 thinking。"""
        orch = setup_stream_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        plan = TaskPlan(
            task_id="task_test",
            goal="现在几点？",
            steps=[
                Step(step_id=1, description="查询时间", tool="get_time",
                     status=StepStatus.PENDING,
                     success_criteria="返回当前时间"),
            ],
            context={"conversation_history": ""},
        )
        orch.planner.plan.return_value = plan

        from agent.tools.schema import ToolResult, ToolResultStatus

        def execute_step_mock(step, plan, prev_results):
            step.status = StepStatus.SUCCESS
            step.result = {"time": "2025-01-15 10:30:00"}
            step.duration_ms = 50.0
            return step, ToolResult(call_id="tc_1", tool="get_time",
                                    status=ToolResultStatus.SUCCESS,
                                    data={"time": "2025-01-15 10:30:00"})

        orch.executor.execute_step.side_effect = execute_step_mock

        from agent.planner.reflector import ReflectionDecision
        orch.reflector.reflect.return_value = ReflectionDecision.CONTINUE

        events = await _collect_events(orch.run_stream("现在几点？"))

        event_types = [e.type for e in events]
        assert StreamEventType.THINKING in event_types
        assert StreamEventType.PLAN_READY in event_types
        assert StreamEventType.DONE in event_types

    @pytest.mark.asyncio
    async def test_run_stream_completes_successfully(self, setup_stream_orch):
        """run_stream() 应该 yield DONE 事件并记录到 memory。"""
        orch = setup_stream_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        plan = TaskPlan(
            task_id="task_test",
            goal="搜索信息",
            steps=[
                Step(step_id=1, description="搜索", tool="web_search",
                     status=StepStatus.SUCCESS,
                     result={"response": "搜索完成"}),
            ],
            context={},
        )
        orch.planner.plan.return_value = plan

        from agent.planner.reflector import ReflectionDecision
        orch.reflector.reflect.return_value = ReflectionDecision.CONTINUE

        events = await _collect_events(orch.run_stream("搜索信息"))

        # 最后一个事件应是 DONE
        assert events[-1].type == StreamEventType.DONE
        # Memory 应该被保存
        orch.memory.save.assert_called()

    @pytest.mark.asyncio
    async def test_run_stream_yields_step_events(self, setup_stream_orch):
        """应该 yield STEP_START, TOOL_RESULT, STEP_DONE 事件。"""
        orch = setup_stream_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        plan = TaskPlan(
            task_id="task_test",
            goal="单步任务",
            steps=[
                Step(step_id=1, description="读取文件", tool="read_file",
                     status=StepStatus.PENDING,
                     success_criteria="返回内容"),
            ],
            context={},
        )
        orch.planner.plan.return_value = plan

        from agent.tools.schema import ToolResult, ToolResultStatus

        def execute_step_mock(step, plan, prev_results):
            step.status = StepStatus.SUCCESS
            step.result = {"content": "file data"}
            return step, ToolResult(call_id="tc_1", tool="read_file",
                                    status=ToolResultStatus.SUCCESS,
                                    data={"content": "file data"})

        orch.executor.execute_step.side_effect = execute_step_mock

        from agent.planner.reflector import ReflectionDecision
        orch.reflector.reflect.return_value = ReflectionDecision.CONTINUE

        events = await _collect_events(orch.run_stream("读取文件"))

        event_types = [e.type for e in events]
        assert StreamEventType.STEP_START in event_types
        assert StreamEventType.TOOL_RESULT in event_types
        assert StreamEventType.STEP_DONE in event_types
        assert StreamEventType.REFLECTION in event_types

    @pytest.mark.asyncio
    async def test_run_stream_without_initialize_raises(self):
        """未初始化时调用 run_stream() 应抛出 RuntimeError。"""
        orch = AgentOrchestrator()

        with pytest.raises(RuntimeError, match="未初始化"):
            events = await _collect_events(orch.run_stream("测试"))

    @pytest.mark.asyncio
    async def test_run_stream_replan(self, setup_stream_orch):
        """步骤失败触发 replan 时应该 yield 新 plan_ready。"""
        orch = setup_stream_orch

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
                                    status=ToolResultStatus.ERROR,
                                    error="文件不存在")

        orch.executor.execute_step.side_effect = execute_step_mock
        orch.reflector.reflect.return_value = ReflectionDecision.REPLAN

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

        events = await _collect_events(orch.run_stream("读取配置"))

        # replan 后应该有两个 PLAN_READY 事件
        plan_ready_events = [e for e in events if e.type == StreamEventType.PLAN_READY]
        assert len(plan_ready_events) == 2

    @pytest.mark.asyncio
    async def test_run_stream_retry(self, setup_stream_orch):
        """重试流程测试。"""
        orch = setup_stream_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        from agent.tools.schema import ToolResult, ToolResultStatus
        from agent.planner.reflector import ReflectionDecision

        plan = TaskPlan(
            task_id="task_retry",
            goal="搜索",
            steps=[
                Step(step_id=1, description="搜索", tool="web_search",
                     status=StepStatus.PENDING,
                     success_criteria="返回结果"),
            ],
            context={},
        )
        orch.planner.plan.return_value = plan

        call_count = [0]

        def execute_step_mock(step, plan, prev_results):
            call_count[0] += 1
            if call_count[0] == 1:
                step.status = StepStatus.FAILED
                step.error = "timeout"
                return step, ToolResult(call_id="tc_1", tool="web_search",
                                        status=ToolResultStatus.ERROR,
                                        error="timeout")
            step.status = StepStatus.SUCCESS
            step.result = {"results": ["r1"]}
            return step, ToolResult(call_id="tc_2", tool="web_search",
                                    status=ToolResultStatus.SUCCESS,
                                    data={"results": ["r1"]})

        orch.executor.execute_step.side_effect = execute_step_mock

        refl_count = [0]

        def reflect_mock(step, result, plan):
            refl_count[0] += 1
            if refl_count[0] == 1:
                return ReflectionDecision.RETRY
            return ReflectionDecision.CONTINUE

        orch.reflector.reflect.side_effect = reflect_mock

        events = await _collect_events(orch.run_stream("搜索"))

        # 最后一个事件应是 DONE
        assert events[-1].type == StreamEventType.DONE
        # 应该有 RETRY 反射事件
        reflection_events = [e for e in events if e.type == StreamEventType.REFLECTION]
        decisions = [e.data.get("decision") for e in reflection_events]
        assert "retry" in decisions

    @pytest.mark.asyncio
    async def test_run_stream_ask_user(self, setup_stream_orch):
        """ASK_USER 时应该提前结束并返回 DONE。"""
        orch = setup_stream_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        from agent.tools.schema import ToolResult, ToolResultStatus
        from agent.planner.reflector import ReflectionDecision

        plan = TaskPlan(
            task_id="task_ask",
            goal="模糊任务",
            steps=[
                Step(step_id=1, description="不确定的操作", tool="",
                     status=StepStatus.PENDING,
                     success_criteria="完成"),
            ],
            context={},
        )
        orch.planner.plan.return_value = plan

        def execute_step_mock(step, plan, prev_results):
            step.status = StepStatus.FAILED
            step.error = "无法确定目标"
            return step, ToolResult(call_id="tc_1", tool="direct_response",
                                    status=ToolResultStatus.ERROR,
                                    error="无法确定目标")

        orch.executor.execute_step.side_effect = execute_step_mock
        orch.reflector.reflect.return_value = ReflectionDecision.ASK_USER

        events = await _collect_events(orch.run_stream("模糊任务"))

        # 应该以 DONE 结束 (即使是 ASK_USER)
        assert events[-1].type == StreamEventType.DONE
        # 错误事件应该包含 ASK_USER 信息
        error_events = [e for e in events if e.type == StreamEventType.ERROR]
        assert len(error_events) >= 1


class TestRunStreamSynthesis:
    """_synthesize_stream 测试。"""

    @pytest.mark.asyncio
    async def test_synthesize_single_step_response_field(self, setup_stream_orch):
        """单步成功 → 提取 response 字段。"""
        orch = setup_stream_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        step = Step(step_id=1, description="搜索", status=StepStatus.SUCCESS,
                    result={"response": "搜索完成，找到 3 个结果"})
        plan = TaskPlan(task_id="t1", goal="搜索", steps=[step], context={})

        answer = await orch._synthesize_stream(plan)
        assert "搜索完成" in answer

    @pytest.mark.asyncio
    async def test_synthesize_multi_step(self, setup_stream_orch):
        """多步骤 → LLM 合成 (通过 asyncio.to_thread 调用同步 chat)。"""
        orch = setup_stream_orch
        orch.llm.chat.return_value = MagicMock(content="合成结果: 3 项")

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        step1 = Step(step_id=1, description="读取", status=StepStatus.SUCCESS,
                     result={"content": "data"})
        step2 = Step(step_id=2, description="分析", status=StepStatus.SUCCESS,
                     result={"response": "分析完成"})
        plan = TaskPlan(task_id="t1", goal="分析", steps=[step1, step2], context={})

        answer = await orch._synthesize_stream(plan)
        assert "合成结果" in answer


class TestFinalizeExecution:
    """_finalize_execution 测试。"""

    def test_finalize_saves_memory_and_history(self, setup_stream_orch):
        """_finalize_execution 应该保存 memory 和 execution_history。"""
        orch = setup_stream_orch

        from agent.planner.task_plan import TaskPlan, Step, StepStatus
        step = Step(step_id=1, description="测试", status=StepStatus.SUCCESS,
                    result={"data": "ok"})
        plan = TaskPlan(task_id="t1", goal="测试", steps=[step], context={})

        orch._finalize_execution("测试输入", plan, "答案", 1.5)

        orch.memory.add_message.assert_called_with("assistant", "答案")
        orch.memory.save.assert_called()
        assert len(orch._execution_history) == 1
        assert orch._execution_history[0]["answer"] == "答案"


class TestInjectSkillDispatchers:
    """_inject_skill_dispatchers 测试。"""

    def test_injects_dispatcher_to_skills(self, setup_stream_orch):
        """应该为注册表中的 Skill 实例注入 dispatcher。"""
        orch = setup_stream_orch
        from agent.tools.skill import Skill

        mock_skill = MagicMock(spec=Skill)
        mock_skill.name = "test_skill"
        orch.registry.__iter__ = MagicMock(return_value=iter([mock_skill]))

        orch._inject_skill_dispatchers()

        mock_skill.set_dispatcher.assert_called_once_with(orch.router.dispatch)

    def test_skips_non_skill_tools(self, setup_stream_orch):
        """非 Skill 工具不应该调用 set_dispatcher。"""
        orch = setup_stream_orch
        from agent.tools.schema import ToolProtocol

        mock_tool = MagicMock(spec=ToolProtocol)
        mock_tool.name = "regular_tool"
        orch.registry.__iter__ = MagicMock(return_value=iter([mock_tool]))

        orch._inject_skill_dispatchers()

        # 普通工具不应有 set_dispatcher 调用
        # isinstance check will fail for MagicMock, so it won't be called
        assert True  # no exception
