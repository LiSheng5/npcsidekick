"""
TaskPlan & Step — 结构化任务计划的数据模型。

这是 Planner 和 Executor 之间的契约：
  Planner 输出 TaskPlan (JSON)
  Executor 消费 TaskPlan 并逐步执行

TaskPlan 格式:
  {
    "task_id": "unique-id",
    "goal": "用户想要什么",
    "steps": [
      {
        "step_id": 1,
        "description": "这一步做什么",
        "tool": "推荐的工具名 (可选)",
        "tool_input": {...},       // 预填的工具参数 (可选)
        "depends_on": [],          // 依赖的 step_id
        "success_criteria": "...", // 如何判断成功
        "fallback": "..."          // 失败时的备选方案
      },
      ...
    ],
    "context": {...}               // Planner 从 Memory 检索的上下文
  }
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from enum import Enum


class StepStatus(str, Enum):
    PENDING    = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS    = "success"
    FAILED     = "failed"
    SKIPPED    = "skipped"    # 因为依赖失败而跳过
    RETRYING   = "retrying"


@dataclass
class Step:
    """
    任务计划中的单个步骤。

    示例:
      Step(
          step_id=1,
          description="读取 config.py 理解当前配置",
          tool="read_file",
          tool_input={"path": "config.py"},
          success_criteria="成功读取文件内容，返回不少于 10 行",
          fallback="如果文件不存在，使用 list_dir 检查目录结构",
      )
    """
    step_id: int
    description: str

    # 工具推荐 (可选 — Executor 可选择不同的工具)
    tool: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)

    # 依赖 & 控制流
    depends_on: List[int] = field(default_factory=list)
    is_parallel: bool = False       # 可与同依赖的步骤并行

    # 成功/失败条件
    success_criteria: str = ""
    fallback: str = ""

    # 运行时状态 (由 Executor 填充)
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    retry_count: int = 0
    duration_ms: float = 0.0
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        return self.status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED)

    @property
    def is_success(self) -> bool:
        return self.status == StepStatus.SUCCESS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class TaskPlan:
    """
    结构化的任务计划 — Planner 的输出, Executor 的输入。

    这是整个架构的核心数据契约。
    """
    task_id: str
    goal: str
    steps: List[Step] = field(default_factory=list)

    # 元数据
    context: Dict[str, Any] = field(default_factory=dict)  # Planner 检索的上下文
    estimated_tools: List[str] = field(default_factory=list)  # 预估需要的工具
    created_at: str = ""

    # 运行时 (由 Executor 更新)
    current_step: int = 0
    total_steps_completed: int = 0
    total_steps_failed: int = 0

    def __post_init__(self):
        if not self.task_id:
            self.task_id = f"task_{uuid.uuid4().hex[:12]}"

    # ── 查询 ──────────────────────────────────────────

    def get_step(self, step_id: int) -> Optional[Step]:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    def get_pending_steps(self) -> List[Step]:
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    def get_failed_steps(self) -> List[Step]:
        return [s for s in self.steps if s.status == StepStatus.FAILED]

    def get_ready_steps(self) -> List[Step]:
        """
        返回所有就绪的步骤 (依赖已完成 + 自身状态为 PENDING)。
        """
        completed_ids = {s.step_id for s in self.steps if s.is_success}
        ready = []
        for s in self.steps:
            if s.status != StepStatus.PENDING:
                continue
            if all(dep in completed_ids for dep in s.depends_on):
                ready.append(s)
        return ready

    def is_complete(self) -> bool:
        """所有步骤都成功或跳过。"""
        return all(s.is_complete for s in self.steps)

    def is_success(self) -> bool:
        return all(s.is_success or s.status == StepStatus.SKIPPED for s in self.steps)

    def progress(self) -> str:
        done = sum(1 for s in self.steps if s.is_complete)
        return f"{done}/{len(self.steps)}"

    # ── 序列化 ────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "context": self.context,
            "estimated_tools": self.estimated_tools,
            "created_at": self.created_at,
            "current_step": self.current_step,
            "total_steps_completed": self.total_steps_completed,
            "total_steps_failed": self.total_steps_failed,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskPlan":
        steps = []
        for s in data.get("steps", []):
            step = Step(
                step_id=s.get("step_id", 0),
                description=s.get("description", ""),
                tool=s.get("tool", ""),
                tool_input=s.get("tool_input", {}),
                depends_on=s.get("depends_on", []),
                is_parallel=s.get("is_parallel", False),
                success_criteria=s.get("success_criteria", ""),
                fallback=s.get("fallback", ""),
            )
            step.status = StepStatus(s.get("status", "pending"))
            step.result = s.get("result")
            step.error = s.get("error")
            step.retry_count = s.get("retry_count", 0)
            steps.append(step)

        return cls(
            task_id=data.get("task_id", ""),
            goal=data.get("goal", ""),
            steps=steps,
            context=data.get("context", {}),
            estimated_tools=data.get("estimated_tools", []),
            created_at=data.get("created_at", ""),
            current_step=data.get("current_step", 0),
            total_steps_completed=data.get("total_steps_completed", 0),
            total_steps_failed=data.get("total_steps_failed", 0),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "TaskPlan":
        return cls.from_dict(json.loads(json_str))

    def __repr__(self) -> str:
        return f"<TaskPlan {self.task_id} goal='{self.goal[:40]}...' steps={len(self.steps)} progress={self.progress()}>"
