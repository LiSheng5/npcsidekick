"""
Tests for agent.planner.task_plan — Step, StepStatus, TaskPlan data models.
"""
import json

import pytest

from agent.planner.task_plan import Step, StepStatus, TaskPlan


class TestStepStatus:
    def test_all_statuses_exist(self):
        assert StepStatus.PENDING == "pending"
        assert StepStatus.IN_PROGRESS == "in_progress"
        assert StepStatus.SUCCESS == "success"
        assert StepStatus.FAILED == "failed"
        assert StepStatus.SKIPPED == "skipped"
        assert StepStatus.RETRYING == "retrying"


class TestStep:
    def test_default_values(self):
        step = Step(step_id=1, description="测试步骤")
        assert step.step_id == 1
        assert step.description == "测试步骤"
        assert step.tool == ""
        assert step.tool_input == {}
        assert step.depends_on == []
        assert step.is_parallel is False
        assert step.success_criteria == ""
        assert step.fallback == ""
        assert step.status == StepStatus.PENDING
        assert step.result is None
        assert step.error is None
        assert step.retry_count == 0

    def test_is_complete_for_terminal_statuses(self):
        for status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED):
            step = Step(step_id=1, description="test", status=status)
            assert step.is_complete is True

    def test_is_complete_for_non_terminal_statuses(self):
        for status in (StepStatus.PENDING, StepStatus.IN_PROGRESS, StepStatus.RETRYING):
            step = Step(step_id=1, description="test", status=status)
            assert step.is_complete is False

    def test_is_success_only_true_for_success(self):
        assert Step(step_id=1, description="test", status=StepStatus.SUCCESS).is_success is True
        assert Step(step_id=1, description="test", status=StepStatus.FAILED).is_success is False
        assert Step(step_id=1, description="test", status=StepStatus.SKIPPED).is_success is False
        assert Step(step_id=1, description="test", status=StepStatus.PENDING).is_success is False

    def test_to_dict_serializes_status_as_string(self):
        step = Step(step_id=1, description="读取文件", status=StepStatus.SUCCESS, result={"data": "ok"})
        d = step.to_dict()
        assert d["status"] == "success"
        assert d["step_id"] == 1
        assert d["description"] == "读取文件"
        assert d["result"] == {"data": "ok"}

    def test_step_accepts_all_fields(self):
        step = Step(
            step_id=3,
            description="运行代码",
            tool="run_code",
            tool_input={"code": "print(1)"},
            depends_on=[1, 2],
            is_parallel=True,
            success_criteria="输出结果不为空",
            fallback="手动执行",
            status=StepStatus.PENDING,
            retry_count=0,
        )
        assert step.tool == "run_code"
        assert step.depends_on == [1, 2]
        assert step.is_parallel is True
        assert step.success_criteria == "输出结果不为空"


class TestTaskPlan:
    def test_auto_generates_task_id_when_empty(self):
        plan = TaskPlan(task_id="", goal="测试")
        assert plan.task_id.startswith("task_")
        assert len(plan.task_id) > 5

    def test_preserves_provided_task_id(self):
        plan = TaskPlan(task_id="custom_123", goal="测试")
        assert plan.task_id == "custom_123"

    def test_get_step_existing(self):
        step = Step(step_id=1, description="S1")
        plan = TaskPlan(task_id="t1", goal="G", steps=[step])
        assert plan.get_step(1) is step

    def test_get_step_non_existing(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[Step(step_id=1, description="S1")])
        assert plan.get_step(99) is None

    def test_get_pending_steps(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.PENDING),
            Step(step_id=2, description="S2", status=StepStatus.SUCCESS),
            Step(step_id=3, description="S3", status=StepStatus.FAILED),
        ])
        pending = plan.get_pending_steps()
        assert len(pending) == 1
        assert pending[0].step_id == 1

    def test_get_failed_steps(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.FAILED),
            Step(step_id=2, description="S2", status=StepStatus.FAILED),
            Step(step_id=3, description="S3", status=StepStatus.SUCCESS),
        ])
        failed = plan.get_failed_steps()
        assert len(failed) == 2

    def test_get_ready_steps_all_deps_met(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.SUCCESS),
            Step(step_id=2, description="S2", depends_on=[1], status=StepStatus.PENDING),
        ])
        ready = plan.get_ready_steps()
        assert len(ready) == 1
        assert ready[0].step_id == 2

    def test_get_ready_steps_unmet_dependency(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.FAILED),
            Step(step_id=2, description="S2", depends_on=[1], status=StepStatus.PENDING),
        ])
        ready = plan.get_ready_steps()
        assert len(ready) == 0

    def test_get_ready_steps_no_deps(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.PENDING),
            Step(step_id=2, description="S2", status=StepStatus.PENDING),
        ])
        ready = plan.get_ready_steps()
        assert len(ready) == 2

    def test_get_ready_steps_skipped_dependency_not_met(self):
        """SKIPPED is not is_success, so deps on SKIPPED steps should not be ready."""
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.SKIPPED),
            Step(step_id=2, description="S2", depends_on=[1], status=StepStatus.PENDING),
        ])
        ready = plan.get_ready_steps()
        assert len(ready) == 0

    def test_is_complete_all_terminal(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.SUCCESS),
            Step(step_id=2, description="S2", status=StepStatus.FAILED),
            Step(step_id=3, description="S3", status=StepStatus.SKIPPED),
        ])
        assert plan.is_complete() is True

    def test_is_complete_with_pending(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.SUCCESS),
            Step(step_id=2, description="S2", status=StepStatus.PENDING),
        ])
        assert plan.is_complete() is False

    def test_is_complete_empty_steps(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[])
        assert plan.is_complete() is True  # vacuously true

    def test_is_success_all_success_or_skipped(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.SUCCESS),
            Step(step_id=2, description="S2", status=StepStatus.SKIPPED),
        ])
        assert plan.is_success() is True

    def test_is_success_with_failed(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.SUCCESS),
            Step(step_id=2, description="S2", status=StepStatus.FAILED),
        ])
        assert plan.is_success() is False

    def test_progress(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[
            Step(step_id=1, description="S1", status=StepStatus.SUCCESS),
            Step(step_id=2, description="S2", status=StepStatus.FAILED),
            Step(step_id=3, description="S3", status=StepStatus.PENDING),
        ])
        assert plan.progress() == "2/3"

    def test_progress_empty(self):
        plan = TaskPlan(task_id="t1", goal="G", steps=[])
        assert plan.progress() == "0/0"

    def test_to_dict(self):
        step = Step(step_id=1, description="读取文件", tool="read_file")
        plan = TaskPlan(task_id="t1", goal="读取配置", steps=[step], estimated_tools=["read_file"])
        d = plan.to_dict()
        assert d["task_id"] == "t1"
        assert d["goal"] == "读取配置"
        assert len(d["steps"]) == 1
        assert d["steps"][0]["description"] == "读取文件"
        assert d["estimated_tools"] == ["read_file"]

    def test_to_json(self):
        plan = TaskPlan(task_id="t1", goal="测试", steps=[Step(step_id=1, description="S1")])
        json_str = plan.to_json()
        assert "测试" in json_str

    def test_from_dict_basic(self):
        data = {
            "task_id": "t99",
            "goal": "测试目标",
            "steps": [{"step_id": 1, "description": "S1", "tool": "read_file"}],
            "estimated_tools": ["read_file"],
        }
        plan = TaskPlan.from_dict(data)
        assert plan.task_id == "t99"
        assert plan.goal == "测试目标"
        assert len(plan.steps) == 1
        assert plan.steps[0].description == "S1"
        assert plan.steps[0].tool == "read_file"

    def test_from_dict_with_status(self):
        data = {
            "task_id": "t1",
            "goal": "G",
            "steps": [{"step_id": 1, "description": "S1", "status": "success", "result": {"ok": True}, "error": None, "retry_count": 2}],
        }
        plan = TaskPlan.from_dict(data)
        assert plan.steps[0].status == StepStatus.SUCCESS
        assert plan.steps[0].result == {"ok": True}
        assert plan.steps[0].retry_count == 2

    def test_from_dict_invalid_status_raises(self):
        data = {
            "task_id": "t1",
            "goal": "G",
            "steps": [{"step_id": 1, "description": "S1", "status": "nonexistent"}],
        }
        with pytest.raises(ValueError):
            TaskPlan.from_dict(data)

    def test_from_dict_missing_keys_uses_defaults(self):
        data = {}
        plan = TaskPlan.from_dict(data)
        # __post_init__ auto-generates task_id when empty
        assert plan.task_id.startswith("task_")
        assert plan.goal == ""
        assert plan.steps == []

    def test_from_json(self):
        json_str = json.dumps({"task_id": "t_json", "goal": "JSON Goal", "steps": []})
        plan = TaskPlan.from_json(json_str)
        assert plan.task_id == "t_json"
        assert plan.goal == "JSON Goal"

    def test_from_json_invalid_raises(self):
        with pytest.raises(json.JSONDecodeError):
            TaskPlan.from_json("not valid json")

    def test_round_trip_to_dict_from_dict(self):
        original = TaskPlan(
            task_id="t_round",
            goal="往返测试",
            steps=[
                Step(step_id=1, description="S1", tool="read_file", status=StepStatus.SUCCESS),
                Step(step_id=2, description="S2", depends_on=[1], status=StepStatus.PENDING),
            ],
            estimated_tools=["read_file"],
            total_steps_completed=1,
        )
        restored = TaskPlan.from_dict(original.to_dict())
        assert restored.task_id == original.task_id
        assert restored.goal == original.goal
        assert len(restored.steps) == 2
        assert restored.steps[0].status == StepStatus.SUCCESS
        assert restored.steps[1].depends_on == [1]
        assert restored.total_steps_completed == 1

    def test_repr(self):
        plan = TaskPlan(task_id="t_abc123", goal="测试目标很长的描述文字", steps=[Step(step_id=1, description="S1")])
        r = repr(plan)
        assert "t_abc123" in r
        assert "steps=1" in r
