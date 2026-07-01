"""
Reflector — LLM 驱动的自我反思与自校正。

Reasonix 启发式设计:
  - 配置驱动的反思提示词 (REFLECTOR_SYSTEM_PROMPT 可从 config 覆盖)
  - 分层评估: 快速规则 → LLM 深度分析 (类似 Reasonix 的 cache-aware 分层上下文)
  - 结构化输出: LLM 通过 function calling 返回确定性决策
  - 优雅降级: LLM 不可用时回退到规则判断

在执行每个步骤后进行反思:
  1. 步骤是否成功?
  2. 结果是否满足成功标准?
  3. 是否需要调整后续步骤?
  4. 是否应该放弃当前策略?

Reflector 输出给 Orchestrator: CONTINUE / RETRY / REPLAN / STOP / ASK_USER
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Optional

from agent.planner.task_plan import Step, StepStatus, TaskPlan
from agent.tools.schema import ToolResult, ToolResultStatus
from agent.llm.client import LLMClient
from agent.logging_config import log as _log
import config


class ReflectionDecision(str, Enum):
    CONTINUE = "continue"      # 继续下一步
    RETRY    = "retry"         # 重试当前步骤
    REPLAN   = "replan"        # 重新规划 (目标不变, 路径调整)
    STOP     = "stop"          # 任务完成或无法继续
    ASK_USER = "ask_user"      # 需要用户输入


# ── LLM Reflection Prompt ──────────────────────────────
# 可通过 config.py 覆盖 (Reasonix 风格: 配置驱动)

REFLECTOR_SYSTEM_PROMPT = """你是任务执行的反思评估专家。你的职责是评估一个 AI Agent 步骤的执行结果，并决定下一步行动。

## 评估维度
1. **目标达成**: 步骤结果是否达到了描述的目标？
2. **成功标准**: 如果定义了成功标准 (success_criteria)，结果是否满足？
3. **结果质量**: 返回的数据是否完整、准确、可用？
4. **错误性质**: 如果有错误，是临时的 (可重试) 还是根本性的 (需重新规划)？

## 决策指南
- **continue**: 步骤成功，结果满足要求，继续执行下一步
- **retry**: 临时性失败 (如网络超时、API 限流、资源暂时不可用)，重试可能成功
- **replan**: 当前方法不可行，需要换工具、换参数、或换策略
- **stop**: 任务已完成 (最后一步成功) 或遇到无法恢复的致命错误
- **ask_user**: 缺少关键信息、权限不足、或需要用户做出选择

## 判断原则
- 工具返回了有效数据且无明显错误 → continue (即使格式不够完美)
- 空列表/空结果不一定是失败 — 可能确实没有匹配数据
- 错误信息中包含 "timeout", "connection", "rate limit", "temporary" → retry
- 错误信息中包含 "not found", "permission denied", "invalid" → replan
- 已重试 >= max_retries 次仍失败 → 不要继续 retry，应 replan 或 ask_user
- 结果部分满足标准但不够好 → 如果已重试多次则 continue (接受不完美)，否则 retry
"""

REFLECTOR_PLAN_PROMPT = """你是任务计划的全局评估专家。评估整个计划的执行状况并决定下一步。

## 决策指南
- **stop**: 所有步骤已完成且结果满意
- **continue**: 还有步骤待执行，当前进展顺利
- **replan**: 部分步骤失败，需要调整剩余计划
- **ask_user**: 超过一半步骤失败，或遇到需要用户决策的问题
"""

# ── Function Calling 工具定义 ──────────────────────────

REFLECTION_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_reflection",
        "description": "输出对步骤执行结果的反思评估",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["continue", "retry", "replan", "stop", "ask_user"],
                    "description": "反思后做出的决策"
                },
                "criteria_met": {
                    "type": "boolean",
                    "description": "步骤的成功标准是否已满足 (没有定义标准则默认 true)"
                },
                "reason": {
                    "type": "string",
                    "description": "做出此决策的简要原因 (1-2 句话)"
                },
                "suggestion": {
                    "type": "string",
                    "description": "如果决策是 retry/replan，建议如何调整 (可选)"
                },
            },
            "required": ["decision", "criteria_met", "reason"],
        },
    },
}

REFLECTION_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_plan_reflection",
        "description": "输出对任务计划全局状态的评估",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["continue", "replan", "stop", "ask_user"],
                    "description": "全局评估后的决策"
                },
                "reason": {
                    "type": "string",
                    "description": "做出此决策的原因"
                },
                "should_drop_failed": {
                    "type": "boolean",
                    "description": "是否应该放弃失败的步骤继续执行剩余步骤",
                },
            },
            "required": ["decision", "reason"],
        },
    },
}


class Reflector:
    """
    LLM 驱动的步骤反射器 — 评估每一步的执行结果并给出决策。

    决策流程 (Reasonix 分层风格):
      1. 快速规则检查 (确定性 — 零延迟)
      2. 成功 + 无成功标准 → CONTINUE (快速路径, 不调用 LLM)
      3. 成功 + 有成功标准 → LLM 深度评估
      4. 错误/超时/其他 → LLM 评估是否可重试

    使用方式:
      reflector = Reflector(llm_client)
      decision = reflector.reflect(step, result, plan)
      if decision == ReflectionDecision.RETRY:
          ...
    """

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm
        self.enabled = config.REFLECTION_ENABLED

        # 允许通过 config 覆盖提示词 (Reasonix 风格: 配置驱动)
        self.system_prompt = getattr(config, 'REFLECTOR_SYSTEM_PROMPT', None) or REFLECTOR_SYSTEM_PROMPT
        self.plan_prompt = getattr(config, 'REFLECTOR_PLAN_PROMPT', None) or REFLECTOR_PLAN_PROMPT

        # 是否启用 LLM 反射 (可通过 config 关闭以节省 token)
        self.use_llm = getattr(config, 'REFLECTION_USE_LLM', True)

    # ── Public API ──────────────────────────────────────

    def reflect(
        self,
        step: Step,
        result: ToolResult,
        plan: TaskPlan,
    ) -> ReflectionDecision:
        """
        反思当前步骤的执行结果。

        决策流程:
          1. 快速规则检查 (确定性)
          2. LLM 深度分析 (如果启用且规则不够)
        """
        if not self.enabled:
            return self._fallback_rules(step, result, plan)

        # ── 快速规则 (确定性 — 零延迟) ─────────────────

        # 被拒绝 (安全检查) → 必定 replan
        if result.status == ToolResultStatus.REJECTED:
            return ReflectionDecision.REPLAN

        # 超时 → retry (如果还有重试次数)
        if result.status == ToolResultStatus.TIMEOUT:
            if step.retry_count < config.MAX_RETRIES:
                return ReflectionDecision.RETRY
            return ReflectionDecision.REPLAN

        # ── LLM 深度分析 ────────────────────────────────

        if result.ok:
            # 快速路径: 成功 + 无成功标准 + 非最后一步 → continue
            if not step.success_criteria:
                if step.step_id == self._last_step_id(plan):
                    return ReflectionDecision.STOP
                return ReflectionDecision.CONTINUE

            # 有成功标准: LLM 检查是否真的满足
            if self.use_llm and self.llm:
                return self._reflect_with_llm(step, result, plan)

            # 回退: 规则检查
            criteria_met = self._check_criteria(step, result)
            if criteria_met:
                if step.step_id == self._last_step_id(plan):
                    return ReflectionDecision.STOP
                return ReflectionDecision.CONTINUE
            if step.retry_count < config.MAX_RETRIES:
                return ReflectionDecision.RETRY
            return ReflectionDecision.REPLAN

        # 步骤失败: LLM 分析错误性质
        if self.use_llm and self.llm:
            return self._reflect_with_llm(step, result, plan)

        # 回退: 规则判断
        return self._fallback_rules(step, result, plan)

    def reflect_plan(
        self,
        plan: TaskPlan,
        failed_steps: list[Step],
    ) -> ReflectionDecision:
        """
        全局反思 — 评估整个计划的状态。

        当多个步骤失败或无就绪步骤时由 Orchestrator 调用。
        """
        if not failed_steps:
            if plan.is_success():
                return ReflectionDecision.STOP
            return ReflectionDecision.CONTINUE

        # LLM 全局评估
        if self.use_llm and self.llm:
            return self._reflect_plan_with_llm(plan, failed_steps)

        # 回退: 规则判断
        fail_ratio = len(failed_steps) / max(len(plan.steps), 1)
        if fail_ratio > 0.5:
            return ReflectionDecision.ASK_USER
        return ReflectionDecision.REPLAN

    # ── LLM-based Reflection ────────────────────────────

    def _reflect_with_llm(
        self,
        step: Step,
        result: ToolResult,
        plan: TaskPlan,
    ) -> ReflectionDecision:
        """
        调用 LLM 对步骤结果进行深度评估。

        发送给 LLM 的上下文 (Reasonix 风格 — 精简但完整):
          - 用户目标
          - 步骤描述 + 成功标准
          - 执行结果 (截断)
          - 重试次数 / 最大重试
          - 计划进度
        """
        # 构建精简上下文
        is_last = step.step_id == self._last_step_id(plan)

        # 截断结果数据以避免 token 浪费
        result_data_str = self._format_result(result)

        context = f"""## 用户目标
{plan.goal}

## 当前步骤
- Step ID: {step.step_id}
- 描述: {step.description}
- 推荐工具: {step.tool or '未指定'}
- 成功标准: {step.success_criteria or '未定义 (默认: 工具返回有效数据即为成功)'}
- 重试次数: {step.retry_count} / {config.MAX_RETRIES}
- 是否为最后一步: {'是' if is_last else '否 (还剩 ' + str(len(plan.steps) - step.step_id) + ' 步)'}

## 执行结果
- 状态: {'成功' if result.ok else '失败 (' + (result.status.value if hasattr(result.status, 'value') else str(result.status)) + ')'}
- 使用的工具: {result.tool}
- 错误信息: {result.error or '无'}
- 结果数据: {result_data_str}"""

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": context},
        ]

        try:
            response = self.llm.chat(
                messages,
                tools=[REFLECTION_TOOL],
                tool_choice={"type": "function", "function": {"name": "emit_reflection"}},
                max_tokens=300,  # 反思不需要长输出
            )
            return self._parse_reflection(response, step, plan)
        except Exception as e:
            # LLM 调用失败 → 回退规则
            _log.warning("reflection_llm_failed", error=str(e), step_id=step.step_id)
            return self._fallback_rules(step, result, plan)

    def _reflect_plan_with_llm(
        self,
        plan: TaskPlan,
        failed_steps: list[Step],
    ) -> ReflectionDecision:
        """调用 LLM 对全局计划状态进行评估。"""
        failed_info = []
        for s in failed_steps:
            failed_info.append(f"- Step {s.step_id}: {s.description[:80]}\n  错误: {s.error or '未知'}\n  重试: {s.retry_count} 次")

        completed_info = []
        for s in plan.steps:
            if s.is_success:
                completed_info.append(f"- Step {s.step_id}: {s.description[:80]} ✅")

        context = f"""## 用户目标
{plan.goal}

## 已完成的步骤
{chr(10).join(completed_info) if completed_info else '无'}

## 失败的步骤
{chr(10).join(failed_info)}

## 计划状态
- 总步骤: {len(plan.steps)}
- 已完成: {plan.total_steps_completed}
- 已失败: {len(failed_steps)}
- 失败比例: {len(failed_steps)}/{len(plan.steps)}"""

        messages = [
            {"role": "system", "content": self.plan_prompt},
            {"role": "user", "content": context},
        ]

        try:
            response = self.llm.chat(
                messages,
                tools=[REFLECTION_PLAN_TOOL],
                tool_choice={"type": "function", "function": {"name": "emit_plan_reflection"}},
                max_tokens=300,
            )
            parsed = self._parse_plan_reflection(response)
            return parsed
        except Exception as e:
            _log.warning("reflection_plan_llm_failed", error=str(e))
            fail_ratio = len(failed_steps) / max(len(plan.steps), 1)
            if fail_ratio > 0.5:
                return ReflectionDecision.ASK_USER
            return ReflectionDecision.REPLAN

    # ── Parsing ─────────────────────────────────────────

    def _parse_reflection(
        self,
        response,
        step: Step,
        plan: TaskPlan,
    ) -> ReflectionDecision:
        """从 LLM 响应中解析 ReflectionDecision。"""
        decision = None
        reason = ""

        # 解析 function calling 响应
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.function.name == "emit_reflection":
                    try:
                        data = json.loads(tc.function.arguments)
                        decision = data.get("decision", "")
                        reason = data.get("reason", "")
                    except json.JSONDecodeError:
                        pass

        # 尝试从 content 中解析 JSON
        if not decision and response.content:
            try:
                data = json.loads(response.content)
                decision = data.get("decision", "")
                reason = data.get("reason", "")
            except json.JSONDecodeError:
                pass

        # 映射到 ReflectionDecision
        decision_map = {
            "continue": ReflectionDecision.CONTINUE,
            "retry": ReflectionDecision.RETRY,
            "replan": ReflectionDecision.REPLAN,
            "stop": ReflectionDecision.STOP,
            "ask_user": ReflectionDecision.ASK_USER,
        }

        if decision and decision in decision_map:
            if reason:
                _log.info("reflection_decision", decision=decision,
                          reason=reason[:100], step_id=step.step_id, used_llm=True)
            return decision_map[decision]

        # 无法解析 → 回退
        _log.warning("reflection_parse_failed", step_id=step.step_id)
        return self._fallback_rules_by_result(step, plan)

    def _parse_plan_reflection(self, response) -> ReflectionDecision:
        """从 LLM 响应中解析计划级 ReflectionDecision。"""
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.function.name == "emit_plan_reflection":
                    try:
                        data = json.loads(tc.function.arguments)
                        decision = data.get("decision", "")
                        reason = data.get("reason", "")
                        if reason:
                            _log.info("reflection_plan_decision", decision=decision,
                                      reason=reason[:100], used_llm=True)
                        mapping = {
                            "continue": ReflectionDecision.CONTINUE,
                            "replan": ReflectionDecision.REPLAN,
                            "stop": ReflectionDecision.STOP,
                            "ask_user": ReflectionDecision.ASK_USER,
                        }
                        if decision in mapping:
                            return mapping[decision]
                    except json.JSONDecodeError:
                        pass
        return ReflectionDecision.REPLAN  # 安全默认值

    # ── Fallback Rules ──────────────────────────────────

    def _fallback_rules(
        self,
        step: Step,
        result: ToolResult,
        plan: TaskPlan,
    ) -> ReflectionDecision:
        """确定性回退规则 — 当 LLM 不可用时使用。"""
        if result.ok:
            is_last = step.step_id == self._last_step_id(plan)
            if step.success_criteria:
                if self._check_criteria(step, result):
                    return ReflectionDecision.STOP if is_last else ReflectionDecision.CONTINUE
                if step.retry_count < config.MAX_RETRIES:
                    return ReflectionDecision.RETRY
                return ReflectionDecision.REPLAN
            return ReflectionDecision.STOP if is_last else ReflectionDecision.CONTINUE

        return self._fallback_rules_by_result(step, plan)

    def _fallback_rules_by_result(
        self,
        step: Step,
        plan: TaskPlan,
    ) -> ReflectionDecision:
        """基于步骤状态的回退规则 (不需要 result 参数)。"""
        if step.retry_count < config.MAX_RETRIES:
            return ReflectionDecision.RETRY
        if step.fallback:
            return ReflectionDecision.REPLAN
        return ReflectionDecision.ASK_USER

    # ── Helpers ─────────────────────────────────────────

    def _check_criteria(self, step: Step, result: ToolResult) -> bool:
        """
        确定性成功标准检查 (启发式)。
        当 LLM 不可用时的回退方案。比之前更全面但仍然有限。
        """
        criteria = step.success_criteria.lower()

        # 检查数据非空
        if any(kw in criteria for kw in ["不为空", "not empty", "not null", "非空"]):
            if result.data is None:
                return False
            if isinstance(result.data, dict):
                if not any(v for v in result.data.values() if v):
                    return False
            if isinstance(result.data, (list, str)) and len(result.data) == 0:
                return False

        # 检查包含特定关键词
        if "包含" in criteria or "contain" in criteria:
            import re
            # 提取引号中的期望关键词
            quoted = re.findall(r'["\']([^"\']+)["\']', criteria)
            if not quoted:
                quoted = re.findall(r'[「]([^」]+)[」]', criteria)
            if quoted:
                result_str = json.dumps(result.data, ensure_ascii=False).lower()
                for q in quoted:
                    if q.lower() not in result_str:
                        return False

        # 检查数量/行数阈值
        if "不少于" in criteria or "至少" in criteria or "at least" in criteria or "不少于" in criteria:
            import re
            nums = re.findall(r"\d+", criteria)
            if nums:
                expected = int(nums[0])
                actual = self._extract_count(result.data)
                if actual < expected:
                    return False

        return True

    def _format_result(self, result: ToolResult) -> str:
        """格式化结果数据，截断以避免浪费 token。"""
        if result.data is None:
            return "(无数据)"
        try:
            raw = json.dumps(result.data, ensure_ascii=False)
            if len(raw) > 800:
                return raw[:800] + f"... (截断, 共 {len(raw)} 字符)"
            return raw
        except (TypeError, ValueError):
            return str(result.data)[:800]

    def _extract_count(self, data) -> int:
        """从结果数据中提取数量。"""
        if isinstance(data, dict):
            for key in ("total_lines", "count", "read_lines", "total", "length", "size"):
                if key in data and isinstance(data[key], (int, float)):
                    return int(data[key])
            for key in ("lines", "items", "results", "matches"):
                if key in data and isinstance(data[key], list):
                    return len(data[key])
        if isinstance(data, list):
            return len(data)
        if isinstance(data, str):
            return len(data)
        return 0

    @staticmethod
    def _last_step_id(plan: TaskPlan) -> int:
        """获取计划中最后一步的 ID。"""
        if not plan.steps:
            return 0
        return max(s.step_id for s in plan.steps)
