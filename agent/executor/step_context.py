"""
StepContext — 每个步骤的独立执行上下文。

确保执行器是"stateless per task step"的:
  - 每一步创建新的 StepContext
  - 步骤间不共享可变状态
  - 结果通过 TaskPlan 传递
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime


@dataclass
class StepContext:
    """
    单步执行上下文 — 每个步骤独立创建。

    包含:
      - 步骤引用
      - 之前的步骤结果 (只读)
      - 当前步骤的临时状态
      - 计时信息
    """
    step_id: int
    step_description: str

    # 来自之前步骤的结果 (只读, key=step_id)
    previous_results: Dict[int, Any] = field(default_factory=dict)

    # 推荐的策略
    recommended_tools: List[str] = field(default_factory=list)

    # 当前步骤的临时变量
    scratchpad: Dict[str, Any] = field(default_factory=dict)
    observations: List[str] = field(default_factory=list)

    # 计时
    started_at: float = field(default_factory=time.time)

    # 结果
    result: Any = None
    error: Optional[str] = None

    @property
    def elapsed_ms(self) -> float:
        return (time.time() - self.started_at) * 1000

    def observe(self, note: str) -> None:
        self.observations.append(f"[{datetime.now().strftime('%H:%M:%S')}] {note}")

    def get_previous_result(self, step_id: int) -> Any:
        return self.previous_results.get(step_id)

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "description": self.step_description,
            "previous_results_keys": list(self.previous_results.keys()),
            "observations": self.observations,
            "elapsed_ms": self.elapsed_ms,
        }
