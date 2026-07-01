"""
Planner — 将用户请求分解为结构化的 TaskPlan。

工作流程:
  1. 从 Memory 检索相关上下文
  2. 让 LLM 分析任务并生成解题步骤
  3. 为每一步推荐最佳工具
  4. 返回结构化 TaskPlan (JSON)

支持:
  - Long-horizon 推理 (最多 MAX_PLAN_STEPS 步)
  - Replanning (当 Executor 报告某步失败时)
  - 上下文注入 (从 Memory 检索的事实/偏好)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from agent.planner.task_plan import TaskPlan, Step, StepStatus
from agent.llm.client import LLMClient, LLMResponse
from agent.tools.registry import ToolRegistry, get_registry
from agent.tools.router import ToolRouter
from agent.memory.memory_manager import MemoryManager
import config


PLANNER_SYSTEM_PROMPT = """你是任务规划专家。你的职责是将用户请求分解为可执行的步骤序列。

## 规划原则

1. **分解**: 将复杂任务拆成独立/有序的小步骤
2. **排序**: 识别步骤间的依赖关系 (哪一步必须先完成)
3. **工具匹配**: 为每一步推荐最合适的工具
4. **成功标准**: 定义每步的成功条件 (能客观判断)
5. **备选方案**: 考虑失败时的替代路径

## 输出格式

你必须用 `emit_task_plan` 函数输出你的计划。格式如下:

{
  "goal": "用户的高层目标",
  "steps": [
    {
      "step_id": 1,
      "description": "这一步做什么 (具体、可操作)",
      "tool": "推荐的第一个工具 (可选)",
      "tool_input": {"key": "value"},
      "depends_on": [],
      "success_criteria": "如何判断成功",
      "fallback": "如果失败, 尝试什么替代方案"
    }
  ]
}

## 规则
- 不要超过 {max_steps} 步
- 如果任务简单, 可以只有 1-2 步
- 每一步必须可验证 (success_criteria 不能模糊)
- 依赖关系必须正确 (depends_on 引用的 step_id 必须存在)
- 从 context 中查阅已有信息, 避免重复工作
- 纯问候/道别 (你好/hi/谢谢/再见/bye) 返回 0 步计划。简短的事实性问题 (几点/今天几号/计算XX/搜索XX) 仍需规划 1 步并推荐对应工具
"""


class Planner:
    """
    任务规划器。

    使用方式:
      planner = Planner(llm_client, memory_manager)
      plan = planner.plan("帮我找一个 bug: 用户登录失败")
      print(plan.to_json())
    """

    def __init__(
        self,
        llm: LLMClient,
        memory: MemoryManager,
        registry: Optional[ToolRegistry] = None,
    ):
        self.llm = llm
        self.memory = memory
        self.registry = registry or get_registry()
        self.router = ToolRouter(self.registry)

    def plan(self, user_input: str) -> TaskPlan:
        """
        分析用户输入，生成 TaskPlan。

        Args:
          user_input: 原始用户输入

        Returns:
          TaskPlan: 结构化的任务计划
        """
        # 1. 检索上下文
        context_str = self.memory.retrieve_for_planning(user_input)

        # 2. 工具推荐
        recommended_tools = self.router.recommend_for_step(user_input)
        tool_descriptions = self.registry.to_tool_descriptions()

        # 3. 构建 prompt
        prompt = PLANNER_SYSTEM_PROMPT.replace("{max_steps}", str(config.MAX_PLAN_STEPS))

        messages = [
            {"role": "system", "content": prompt},
            {"role": "system", "content": f"## 可用工具\n{tool_descriptions}"},
            {"role": "system", "content": f"## 相关上下文\n{context_str}" if context_str else "## 相关上下文\n暂无相关上下文。"},
            {"role": "system", "content": f"## 推荐工具\n{', '.join(recommended_tools) if recommended_tools else '所有工具均可使用'}"},
            {"role": "user", "content": f"请为以下用户请求生成任务计划:\n\n{user_input}"},
        ]

        # 4. 调用 LLM
        plan_tool = {
            "type": "function",
            "function": {
                "name": "emit_task_plan",
                "description": "输出结构化的任务执行计划",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "用户的高层目标"},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step_id": {"type": "integer"},
                                    "description": {"type": "string"},
                                    "tool": {"type": "string"},
                                    "tool_input": {"type": "object"},
                                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                                    "success_criteria": {"type": "string"},
                                    "fallback": {"type": "string"},
                                },
                                "required": ["step_id", "description", "success_criteria"],
                            },
                        },
                    },
                    "required": ["goal", "steps"],
                },
            },
        }

        try:
            response = self.llm.chat(
                messages,
                tools=[plan_tool],
                tool_choice={"type": "function", "function": {"name": "emit_task_plan"}},
            )
            plan_data = self._parse_plan(response)
        except Exception as e:
            # Fallback: 简单计划
            plan_data = {
                "goal": user_input,
                "steps": [{"step_id": 1, "description": "直接处理用户请求", "success_criteria": "用户获得满意的回复"}],
            }

        # 5. 获取对话历史 (Reasonix 风格: 注入到 plan.context 供 Executor 使用)
        conversation_history = self.memory.get_history_for_context(max_messages=20, max_chars=4000)

        # 6. 构建 TaskPlan
        return self._build_task_plan(plan_data, context_str, conversation_history)

    def replan(self, original_plan: TaskPlan, failed_step: Step, error: str) -> TaskPlan:
        """
        根据失败信息重新规划。
        保持已成功的步骤，为失败步骤生成替代方案。

        Args:
          original_plan: 原计划
          failed_step: 失败的步骤
          error: 错误信息

        Returns:
          TaskPlan: 修正后的计划 (已成功的步骤保留)
        """
        completed_steps = [s for s in original_plan.steps if s.is_success]
        remaining_steps = [s for s in original_plan.steps if not s.is_complete and s.step_id != failed_step.step_id]

        messages = [
            {"role": "system", "content": "你需要在步骤失败后进行重新规划。已完成的步骤保持不变，为失败步骤设计替代方案。"},
            {"role": "system", "content": f"## 可用工具\n{self.registry.to_tool_descriptions()}"},
            {"role": "user", "content": f"""原始目标: {original_plan.goal}

已完成的步骤:
{json.dumps([s.to_dict() for s in completed_steps], ensure_ascii=False, indent=2)}

失败的步骤:
- Step {failed_step.step_id}: {failed_step.description}
- 错误: {error}
- 原备选方案: {failed_step.fallback}

剩余步骤:
{json.dumps([s.to_dict() for s in remaining_steps], ensure_ascii=False, indent=2)}

请为失败的步骤生成新的替代步骤 (1-3 步)，并整合剩余步骤。输出格式与初次规划相同。"""},
        ]

        plan_tool = {
            "type": "function",
            "function": {
                "name": "emit_task_plan",
                "description": "输出修正后的任务计划",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["goal", "steps"],
                },
            },
        }

        try:
            response = self.llm.chat(
                messages,
                tools=[plan_tool],
                tool_choice={"type": "function", "function": {"name": "emit_task_plan"}},
            )
            new_data = self._parse_plan(response)
        except Exception:
            # Fallback: 跳过失败步骤, 继续剩余步骤
            new_data = {
                "goal": original_plan.goal,
                "steps": [{
                    "step_id": failed_step.step_id,
                    "description": f"跳过(已失败): {failed_step.description}. 错误: {error}",
                    "success_criteria": "标记为跳过",
                }],
            }

        # 合并: 已成功 + 新替代步骤
        new_plan = self._build_task_plan(new_data, "")
        new_plan.task_id = original_plan.task_id  # 保持同一任务
        # 恢复已完成的步骤
        final_steps = completed_steps + new_plan.steps
        new_plan.steps = final_steps
        return new_plan

    # ── internal ──────────────────────────────────────

    def _parse_plan(self, response: LLMResponse) -> dict:
        """从 LLM 响应中解析 plan JSON。"""
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.function.name == "emit_task_plan":
                    try:
                        return json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        pass

        # 尝试从 content 中解析 JSON
        if response.content:
            try:
                return json.loads(response.content)
            except json.JSONDecodeError:
                # 尝试提取 {...}
                import re
                match = re.search(r"\{[\s\S]*\}", response.content)
                if match:
                    try:
                        return json.loads(match.group())
                    except json.JSONDecodeError:
                        pass

        # 完全失败 — 返回空计划
        return {"goal": "unknown", "steps": []}

    def _build_task_plan(self, plan_data: dict, context_str: str,
                          conversation_history: str = "") -> TaskPlan:
        """从解析的 JSON 构建 TaskPlan 对象。"""
        steps = []
        for s in plan_data.get("steps", []):
            step = Step(
                step_id=s.get("step_id", len(steps) + 1),
                description=s.get("description", ""),
                tool=s.get("tool", ""),
                tool_input=s.get("tool_input", {}),
                depends_on=s.get("depends_on", []),
                is_parallel=s.get("is_parallel", False),
                success_criteria=s.get("success_criteria", ""),
                fallback=s.get("fallback", ""),
            )
            steps.append(step)

        # 如果没有步骤，生成一个默认步骤
        if not steps:
            steps.append(Step(
                step_id=1,
                description="直接回复用户 (无需使用工具)",
                success_criteria="用户获得满意的回复",
            ))

        return TaskPlan(
            task_id=f"task_{datetime.now().strftime('%Y%m%d%H%M%S')}_{len(steps)}steps",
            goal=plan_data.get("goal", ""),
            steps=steps,
            context={
                "retrieved_context": context_str,
                "conversation_history": conversation_history,  # Reasonix: 注入给 Executor
            },
            estimated_tools=list(set(s.tool for s in steps if s.tool)),
            created_at=datetime.now().isoformat(),
        )
