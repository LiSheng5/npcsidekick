"""
Reflector — 自我反思与自校正。

在执行每个步骤后进行反思:
  1. 步骤是否成功?
  2. 结果是否满足成功标准?
  3. 是否需要调整后续步骤?
  4. 是否应该放弃当前策略?

Reflector 输出给 Orchestrator: CONTINUE / RETRY / REPLAN / STOP / ASK_USER
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from agent.planner.task_plan import Step, StepStatus, TaskPlan
from agent.tools.schema import ToolResult, ToolResultStatus
from agent.llm.client import LLMClient
import config


class ReflectionDecision(str, Enum):
    CONTINUE = "continue"      # 继续下一步
    RETRY    = "retry"         # 重试当前步骤
    REPLAN   = "replan"        # 重新规划 (目标不变, 路径调整)
    STOP     = "stop"          # 任务完成或无法继续
    ASK_USER = "ask_user"      # 需要用户输入


class Reflector:
    """
    步骤反射器 — 评估每一步的执行结果并给出决策。

    使用方式:
      reflector = Reflector()
      decision = reflector.reflect(step, result, plan)
      if decision == ReflectionDecision.RETRY:
          ...
    """

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm
        self.enabled = config.REFLECTION_ENABLED

    def reflect(
        self,
        step: Step,
        result: ToolResult,
        plan: TaskPlan,
    ) -> ReflectionDecision:
        """
        反思当前步骤的执行结果。

        决策逻辑:
          1. 快速规则检查 (确定性)
          2. 如果规则不够, 调用 LLM 深入分析
        """
        if not self.enabled:
            # 简单规则: 成功 → continue, 失败 → retry or replan
            if result.ok:
                return ReflectionDecision.CONTINUE
            if step.retry_count < config.MAX_RETRIES:
                return ReflectionDecision.RETRY
            return ReflectionDecision.REPLAN

        # ── 快速规则 ──────────────────────────────────

        # 成功
        if result.ok:
            # 检查是否满足成功标准
            if step.success_criteria:
                criteria_met = self._check_criteria(step, result)
                if not criteria_met and step.retry_count < config.MAX_RETRIES:
                    return ReflectionDecision.RETRY
            # 检查是否是最后一步
            if step.step_id == max(s.step_id for s in plan.steps):
                return ReflectionDecision.STOP
            return ReflectionDecision.CONTINUE

        # 超时 → retry
        if result.status == ToolResultStatus.TIMEOUT:
            if step.retry_count < config.MAX_RETRIES:
                return ReflectionDecision.RETRY
            return ReflectionDecision.REPLAN

        # 被拒绝 (安全检查) → replan
        if result.status == ToolResultStatus.REJECTED:
            return ReflectionDecision.REPLAN

        # 错误 → retry or replan
        if step.retry_count < config.MAX_RETRIES:
            return ReflectionDecision.RETRY

        # 重试次数耗尽 → replan
        if step.fallback:
            return ReflectionDecision.REPLAN

        # 彻底失败
        return ReflectionDecision.ASK_USER

    def _check_criteria(self, step: Step, result: ToolResult) -> bool:
        """
        快速检查成功标准 (确定性启发式)。
        对于复杂判断, 应调用 LLM。
        """
        criteria = step.success_criteria.lower()

        # 检查数据完整性
        if "不为空" in criteria or "not empty" in criteria or "not null" in criteria:
            if result.data is None:
                return False
            if isinstance(result.data, dict):
                if not any(v for v in result.data.values() if v):
                    return False

        # 检查行数/数量
        if "不少于" in criteria or "at least" in criteria:
            try:
                # 尝试从 criteria 中提取期望的数字
                import re
                nums = re.findall(r"\d+", criteria)
                if nums:
                    expected = int(nums[0])
                    if isinstance(result.data, dict):
                        actual = result.data.get("total_lines", result.data.get("count", result.data.get("read_lines", 0)))
                        if actual < expected:
                            return False
            except (ValueError, IndexError):
                pass

        return True

    def reflect_plan(
        self,
        plan: TaskPlan,
        failed_steps: list[Step],
    ) -> ReflectionDecision:
        """
        全局反思 — 评估整个计划是否需要重做。
        """
        if not failed_steps:
            if plan.is_success():
                return ReflectionDecision.STOP
            return ReflectionDecision.CONTINUE

        # 超过一半的步骤失败 → 可能需要 ask_user
        fail_ratio = len(failed_steps) / max(len(plan.steps), 1)
        if fail_ratio > 0.5:
            return ReflectionDecision.ASK_USER

        # 有失败但可以重试
        return ReflectionDecision.REPLAN
