"""
Executor — 逐步执行 TaskPlan。

职责:
  1. 从 TaskPlan 中取出就绪步骤
  2. 创建 StepContext (独立执行上下文)
  3. 解析工具选择 (LLM 动态选择 or 使用 Planner 推荐)
  4. 通过 ToolRouter 执行工具
  5. 记录结果，交给 Reflector 评估
  6. 支持重试和自校正

状态: 无状态 — 每一步创建新 StepContext, 不保留跨步可变状态。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from agent.planner.task_plan import TaskPlan, Step, StepStatus
from agent.executor.step_context import StepContext
from agent.executor.retry import RetryPolicy, ExponentialBackoff
from agent.tools.schema import ToolCall, ToolResult, ToolResultStatus
from agent.tools.router import ToolRouter
from agent.tools.registry import ToolRegistry, get_registry
from agent.llm.client import LLMClient, LLMResponse
from agent.tools.adapters.openai_adapter import OpenAIAdapter
import config


EXECUTOR_SYSTEM_PROMPT = """你是步骤执行专家。你会收到一个具体的步骤描述和可用的工具列表。

## 执行规则

1. 仔细阅读步骤描述
2. 选择最合适的工具来完成任务
3. 正确填写工具参数
4. 如果你已经可以从已有信息直接给出答案, 不要调用工具
5. 如果步骤描述不需要工具, 直接回复

## 当前可用工具
{tool_descriptions}

## 上下文
{context}

请执行当前步骤。如果需要工具就调用, 不需要就直接给出结果。"""


class Executor:
    """
    任务执行器 — 逐步执行 TaskPlan。

    使用方式:
      executor = Executor(llm_client, tool_router)
      step, result = executor.execute_step(step, plan, previous_results)
      if not result.ok:
          # Reflector decides: retry / replan / ask_user
    """

    def __init__(
        self,
        llm: LLMClient,
        router: ToolRouter,
        registry: Optional[ToolRegistry] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        self.llm = llm
        self.router = router
        self.registry = registry or get_registry()
        self.retry_policy = retry_policy or ExponentialBackoff(max_attempts=config.MAX_RETRIES)
        self.adapter = OpenAIAdapter(self.registry)

    def execute_step(
        self,
        step: Step,
        plan: TaskPlan,
        previous_results: Dict[int, Any],
    ) -> Tuple[Step, ToolResult]:
        """
        执行单步 — 包括重试循环。

        Args:
          step: 待执行的步骤
          plan: 所属的任务计划 (只读)
          previous_results: 之前步骤的结果 (key=step_id)

        Returns:
          (更新后的 step, tool_result)
        """
        step.status = StepStatus.IN_PROGRESS
        step.started_at = datetime.now().isoformat()

        # 创建独立上下文
        ctx = StepContext(
            step_id=step.step_id,
            step_description=step.description,
            previous_results=previous_results,
            recommended_tools=[step.tool] if step.tool else [],
        )

        # ── 重试循环 ──────────────────────────────────
        last_result: Optional[ToolResult] = None

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            step.retry_count = attempt - 1

            if attempt > 1:
                step.status = StepStatus.RETRYING
                wait = self.retry_policy.wait_seconds(attempt)
                if wait > 0:
                    time.sleep(wait)
                ctx.observe(f"重试 #{attempt} (第 {attempt - 1} 次失败: {last_result.error if last_result else 'unknown'})")

            # 决定是调 LLM 还是直接使用推荐工具
            result = self._execute_with_llm(step, ctx, plan)

            if result.ok:
                step.status = StepStatus.SUCCESS
                step.result = result.data
                step.duration_ms = ctx.elapsed_ms
                step.completed_at = datetime.now().isoformat()
                return step, result

            last_result = result
            step.error = result.error

            # 如果工具被拒绝 (安全检查), 立即停止重试
            if result.status == ToolResultStatus.REJECTED:
                break

            # 如果不再重试, 停止
            if not self.retry_policy.should_retry(attempt, result.error):
                break

        # ── 所有重试已耗尽 ─────────────────────────────
        step.status = StepStatus.FAILED
        step.duration_ms = ctx.elapsed_ms
        step.completed_at = datetime.now().isoformat()
        if last_result:
            step.error = last_result.error
        return step, last_result or ToolResult(
            call_id="", tool=step.tool or "unknown",
            status=ToolResultStatus.ERROR, error="执行失败 (无结果)",
        )

    def execute_pending_steps(
        self,
        plan: TaskPlan,
    ) -> List[Tuple[Step, ToolResult]]:
        """
        执行所有就绪步骤。由 Orchestrator 调用。
        返回 [(step, result), ...]
        """
        results = []
        previous = {
            s.step_id: s.result
            for s in plan.steps if s.is_success
        }

        ready = plan.get_ready_steps()
        for step in ready:
            updated_step, result = self.execute_step(step, plan, previous)
            results.append((updated_step, result))
            if updated_step.is_success:
                previous[updated_step.step_id] = updated_step.result

        return results

    # ── internal ──────────────────────────────────────

    def _execute_with_llm(
        self,
        step: Step,
        ctx: StepContext,
        plan: TaskPlan,
    ) -> ToolResult:
        """让 LLM 决定如何执行这一步。"""

        # 构建执行上下文 — 对话历史在目标之后
        context_parts = [f"## 用户原始目标\n{plan.goal}"]

        # 注入对话历史 (从 Planner 传递，包含最近的消息)
        conversation_history = plan.context.get("conversation_history", "")
        if conversation_history:
            context_parts.append(f"## 对话历史 (上下文)\n{conversation_history}")

        if ctx.previous_results:
            context_parts.append("## 之前步骤的结果")
            for sid, res in ctx.previous_results.items():
                prev_step = plan.get_step(sid)
                desc = prev_step.description if prev_step else f"Step {sid}"
                res_str = str(res)[:500]
                context_parts.append(f"- Step {sid} ({desc}): {res_str}")

        context_str = "\n".join(context_parts)

        # 如果步骤明确推荐了工具且不需要 LLM 参与决策
        if step.tool and not self._needs_llm_decision(step):
            tool_schema = self.registry.get_schema(step.tool)
            required_params = tool_schema.parameters.get("required", []) if tool_schema else []
            # 如果提供了工具输入，或者该工具没有必需参数 → 直接分发
            if step.tool_input or not required_params:
                call = ToolCall(
                    tool=step.tool,
                    input=step.tool_input or {},
                    reason=step.description,
                )
                return self.router.dispatch(call)

        # 构建消息
        system_prompt = EXECUTOR_SYSTEM_PROMPT.format(
            tool_descriptions=self.registry.to_tool_descriptions(),
            context=context_str,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请执行以下步骤:\n\n**步骤 {step.step_id}**: {step.description}\n\n成功标准: {step.success_criteria or '合理完成即可'}"},
        ]

        # 调用 LLM
        openai_tools = self.adapter.to_openai_tools()
        try:
            response = self.llm.chat(messages, tools=openai_tools, tool_choice="auto")
        except Exception as e:
            return ToolResult(
                call_id="", tool=step.tool or "llm",
                status=ToolResultStatus.ERROR, error=f"LLM 调用失败: {e}",
            )

        # 如果 LLM 没有调用工具，返回文本响应
        if not response.has_tool_calls:
            ctx.observe("LLM 直接回复 (无需工具)")
            return ToolResult(
                call_id="", tool="direct_response",
                status=ToolResultStatus.SUCCESS,
                data={"response": response.content},
            )

        # 解析 tool calls
        tool_call = self.adapter.from_openai_response(response.tool_calls[0])
        tool_call.reason = step.description

        ctx.observe(f"LLM 选择工具: {tool_call.tool}")

        # 执行
        return self.router.dispatch(tool_call)

    def _needs_llm_decision(self, step: Step) -> bool:
        """判断步骤是否需要 LLM 参与决策。"""
        # 如果步骤描述包含 "分析" "判断" "决定" 等词，需要 LLM
        decision_keywords = ["分析", "判断", "决定", "检查", "理解", "评估", "选择",
                            "analyze", "decide", "check", "understand", "evaluate"]
        desc_lower = step.description.lower()
        if any(kw in desc_lower for kw in decision_keywords):
            return True

        # 如果没有指定工具，需要 LLM
        if not step.tool:
            return True

        return False
