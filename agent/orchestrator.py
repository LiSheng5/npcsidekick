"""
AgentOrchestrator — 主编排器，连接所有 4 层。

这是整个架构的"大脑"，负责:
  1. 接收用户输入
  2. 调用 Planner 生成 TaskPlan
  3. 调用 Executor 逐步执行
  4. 在每步后调用 Reflector 评估
  5. 根据 Reflection 决定: continue / retry / replan / stop / ask_user
  6. 合成最终答案并写入 Memory

架构:
  User Input
    → Planner.plan() → TaskPlan
    → [Loop]
      → Executor.execute_step() → (step, result)
      → Reflector.reflect() → decision
      → if RETRY: re-enter loop
      → if REPLAN: Planner.replan()
      → if STOP: break
      → Memory.log_execution()
    → synthesize answer → return to User
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable

from agent.planner.planner import Planner
from agent.planner.task_plan import TaskPlan, Step, StepStatus
from agent.planner.reflector import Reflector, ReflectionDecision
from agent.executor.executor import Executor
from agent.executor.step_context import StepContext
from agent.tools.router import ToolRouter
from agent.tools.registry import ToolRegistry, get_registry
from agent.tools.adapters.openai_adapter import OpenAIAdapter
from agent.tools.schema import ToolCall, ToolResult, ToolResultStatus
from agent.memory.memory_manager import MemoryManager
from agent.llm.client import LLMClient
import config


class AgentOrchestrator:
    """
    主编排器 — 整个 Agent 系统的入口。

    使用方式:
      orch = AgentOrchestrator()
      orch.initialize()          # 注册所有内置工具
      answer = orch.run("帮我找出 login 函数的 bug")
      print(answer)
    """

    def __init__(self):
        # 核心组件 (初始化时创建)
        self.llm: Optional[LLMClient] = None
        self.memory: Optional[MemoryManager] = None
        self.registry: Optional[ToolRegistry] = None
        self.router: Optional[ToolRouter] = None
        self.planner: Optional[Planner] = None
        self.executor: Optional[Executor] = None
        self.reflector: Optional[Reflector] = None
        self.adapter: Optional[OpenAIAdapter] = None

        # 执行历史
        self.current_plan: Optional[TaskPlan] = None
        self._execution_history: List[Dict] = []

        # 回调
        self.on_step_start: Optional[Callable] = None
        self.on_step_complete: Optional[Callable] = None
        self.on_tool_call: Optional[Callable] = None
        self.on_error: Optional[Callable] = None

    def initialize(self) -> None:
        """
        初始化所有组件。
        必须在使用 run() 之前调用。
        """
        self.llm = LLMClient()
        self.memory = MemoryManager()
        self.registry = get_registry()
        self.router = ToolRouter(self.registry)
        self.adapter = OpenAIAdapter(self.registry)

        # 注册内置工具
        self._register_builtin_tools()

        self.planner = Planner(self.llm, self.memory, self.registry)
        self.executor = Executor(self.llm, self.router, self.registry)
        self.reflector = Reflector(self.llm)

    def run(self, user_input: str) -> str:
        """
        运行完整的 Agent 循环。

        Args:
          user_input: 用户输入

        Returns:
          str: 最终答案
        """
        if not self.llm:
            raise RuntimeError("Agent 未初始化。请先调用 initialize()。")

        start_time = time.time()

        # 记录用户输入
        self.memory.add_message("user", user_input)

        # ── Phase 1: PLAN ──────────────────────────────
        print(f"\n{'='*60}")
        print(f"🧠 PLANNER: 分析任务...")
        print(f"{'='*60}")

        plan = self.planner.plan(user_input)
        self.current_plan = plan
        print(f"📋 目标: {plan.goal}")
        print(f"📋 步骤: {len(plan.steps)} 步")
        for s in plan.steps:
            deps = f" (依赖: {s.depends_on})" if s.depends_on else ""
            tool = f" [{s.tool}]" if s.tool else ""
            print(f"  Step {s.step_id}: {s.description[:80]}{tool}{deps}")

        # ── Phase 2: EXECUTE ───────────────────────────
        print(f"\n{'='*60}")
        print(f"⚡ EXECUTOR: 逐步执行...")
        print(f"{'='*60}")

        previous_results: Dict[int, Any] = {}
        max_iterations = config.MAX_PLAN_STEPS * 2  # 安全上限
        iteration = 0

        while not plan.is_complete() and iteration < max_iterations:
            iteration += 1

            # 获取就绪步骤
            ready = plan.get_ready_steps()
            if not ready:
                # 可能所有步骤都失败了
                failed = plan.get_failed_steps()
                if failed:
                    print(f"⚠️  无就绪步骤，但有 {len(failed)} 个失败步骤，尝试 replan...")
                    decision = self.reflector.reflect_plan(plan, failed)
                else:
                    break  # 全部完成

            for step in ready:
                # 回调
                if self.on_step_start:
                    self.on_step_start(step)

                print(f"\n▶ Step {step.step_id}: {step.description[:80]}")
                plan.current_step = step.step_id

                # 执行步骤 (包含重试循环)
                updated_step, result = self._execute_single_step(step, plan, previous_results)

                if self.on_step_complete:
                    self.on_step_complete(updated_step, result)

                # 更新结果
                if updated_step.is_success:
                    previous_results[updated_step.step_id] = updated_step.result
                    plan.total_steps_completed += 1
                    print(f"  ✅ Step {step.step_id} 成功 ({updated_step.duration_ms:.0f}ms)")
                else:
                    plan.total_steps_failed += 1
                    print(f"  ❌ Step {step.step_id} 失败: {updated_step.error}")

                # ── REFLECT ─────────────────────────────
                decision = self.reflector.reflect(updated_step, result, plan)

                if decision == ReflectionDecision.CONTINUE:
                    continue

                elif decision == ReflectionDecision.RETRY:
                    step.status = StepStatus.RETRYING
                    # 步骤会被 get_ready_steps 再次取出并重试
                    # (但此时它是 RETRYING 状态，需要重置为 PENDING)
                    step.status = StepStatus.PENDING
                    continue

                elif decision == ReflectionDecision.REPLAN:
                    print(f"  🔄 触发 replan...")
                    plan = self.planner.replan(plan, updated_step, updated_step.error or "未知错误")
                    self.current_plan = plan
                    break  # 退出 ready 循环，重新获取就绪步骤

                elif decision == ReflectionDecision.ASK_USER:
                    return self._synthesize(
                        plan,
                        f"我需要更多信息才能继续。{step.description} 失败了: {updated_step.error}",
                    )

                elif decision == ReflectionDecision.STOP:
                    break

        # ── Phase 3: SYNTHESIZE ────────────────────────
        answer = self._synthesize(plan, None)
        duration = time.time() - start_time

        # 记录到记忆
        self.memory.add_message("assistant", answer)
        self.memory.log_execution({
            "task_id": plan.task_id,
            "goal": plan.goal,
            "steps_total": len(plan.steps),
            "steps_completed": plan.total_steps_completed,
            "steps_failed": plan.total_steps_failed,
            "duration_sec": duration,
            "final_answer": answer[:200],
            "plan_json": plan.to_json() if plan.total_steps_failed > 0 else None,
        })
        self.memory.save()

        # 执行历史
        self._execution_history.append({
            "input": user_input,
            "plan": plan.to_dict(),
            "answer": answer,
            "duration_sec": duration,
            "timestamp": datetime.now().isoformat(),
        })

        print(f"\n{'='*60}")
        print(f"✅ 完成 ({duration:.1f}s) — {plan.total_steps_completed}/{len(plan.steps)} 步成功")
        print(f"{'='*60}")
        return answer

    def run_chat(self, user_input: str) -> str:
        """
        Chat 模式 — 对于简单对话不启动完整计划循环。
        """
        # 快速检测: 是否是简单问候/闲聊
        simple_patterns = ["你好", "hi", "hello", "嘿", "嗨", "在吗", "谢谢", "bye", "再见", "exit"]
        if any(user_input.lower().strip().lstrip('﻿') == p for p in simple_patterns):
            self.memory.add_message("user", user_input)
            answer = "你好！有什么我可以帮你的吗？"
            self.memory.add_message("assistant", answer)
            self.memory.save()
            return answer

        # 检测是否明确需要工具
        needs_tools = any(kw in user_input.lower() for kw in [
            "计算", "文件", "代码", "搜索", "bug", "debug", "修复",
            "查找", "读取", "写入", "运行", "执行", "查询",
            "calculate", "file", "code", "search", "fix", "read", "write", "run",
        ])

        if needs_tools:
            return self.run(user_input)

        # 简单问题: 直接用 LLM 回答
        self.memory.add_message("user", user_input)
        memory_context = self.memory.retrieve_for_planning(user_input)
        messages = [
            {"role": "system", "content": "你是 Claude Agent 助手。根据记忆中的上下文回答用户问题。保持简洁、有帮助。"},
            {"role": "system", "content": f"## 关于用户的记忆\n{memory_context}" if "暂无" not in memory_context else ""},
        ]
        messages.extend(self.memory.recent_messages(10))
        messages.append({"role": "user", "content": user_input})

        try:
            response = self.llm.chat(messages)
            answer = response.content or "抱歉，我没有理解你的请求。"
        except Exception as e:
            answer = f"处理请求时出现错误: {e}"

        self.memory.add_message("assistant", answer)
        self.memory.save()
        return answer

    # ── 查询 ──────────────────────────────────────────

    def get_execution_summary(self) -> Dict[str, Any]:
        """获取当前执行的摘要。"""
        if not self.current_plan:
            return {"status": "无活动计划"}
        return {
            "plan": self.current_plan.to_dict(),
            "progress": self.current_plan.progress(),
            "router_stats": {
                "total_calls": self.router.total_calls(),
            },
        }

    def get_last_execution(self) -> Optional[Dict]:
        return self._execution_history[-1] if self._execution_history else None

    def get_memory_summary(self) -> str:
        return self.memory.summarize() if self.memory else ""

    # ── internal ──────────────────────────────────────

    def _execute_single_step(
        self,
        step: Step,
        plan: TaskPlan,
        previous_results: Dict[int, Any],
    ) -> tuple[Step, ToolResult]:
        """执行单步并处理回调。"""
        updated_step, result = self.executor.execute_step(step, plan, previous_results)

        if self.on_tool_call and result:
            self.on_tool_call(result.tool, result)

        return updated_step, result

    def _synthesize(self, plan: TaskPlan, override_answer: Optional[str] = None) -> str:
        """
        合成最终答案。
        如果所有步骤成功，让 LLM 基于步骤结果生成自然语言答案。
        """
        if override_answer:
            return override_answer

        # 收集所有成功步骤的结果
        results_text = []
        for s in plan.steps:
            if s.is_success and s.result:
                result_str = json.dumps(s.result, ensure_ascii=False)[:500]
                results_text.append(f"Step {s.step_id} ({s.description[:60]}): {result_str}")
            elif s.status == StepStatus.FAILED:
                results_text.append(f"Step {s.step_id} ({s.description[:60]}): ❌ 失败 — {s.error}")

        if not results_text:
            return "任务已完成，但没有产生具体结果。"

        results_combined = "\n".join(results_text)

        # 如果只有 1 步且成功，直接返回结果
        if len(plan.steps) == 1 and plan.steps[0].is_success:
            result = plan.steps[0].result
            if isinstance(result, dict):
                # 直接提取有意义的内容
                for key in ["response", "content", "data", "result"]:
                    if key in result:
                        return str(result[key])
                return json.dumps(result, ensure_ascii=False)[:500]
            return str(result)[:1000]

        # 多步骤: 让 LLM 合成
        try:
            messages = [
                {"role": "system", "content": "你是一个结果总结助手。请根据以下步骤执行结果，生成一个简洁、有帮助的最终答案。用自然语言回答。"},
                {"role": "user", "content": f"原始目标: {plan.goal}\n\n执行结果:\n{results_combined}\n\n请给出最终答案。"},
            ]
            response = self.llm.chat(messages)
            return response.content or "任务执行完成。请查看上述步骤结果。"
        except Exception:
            return f"任务执行完成。以下是步骤结果:\n\n{results_combined}"

    def _register_builtin_tools(self) -> None:
        """注册所有内置工具到 ToolRegistry。"""
        from agent.tools.builtin.file_tools import ReadFileTool, WriteFileTool, ListDirTool
        from agent.tools.builtin.code_tools import RunCodeTool, LintCodeTool
        from agent.tools.builtin.web_tools import WebSearchTool, WebFetchTool
        from agent.tools.builtin.system_tools import GetTimeTool, CalculatorTool
        from agent.tools.builtin.memory_tools import SaveNoteTool, ListNotesTool, RememberFactTool, SearchMemoryTool, SummarizeContextTool

        self.registry.register_many([
            # File
            ReadFileTool(), WriteFileTool(), ListDirTool(),
            # Code
            RunCodeTool(), LintCodeTool(),
            # Web
            WebSearchTool(), WebFetchTool(),
            # System
            GetTimeTool(), CalculatorTool(),
            # Memory
            SaveNoteTool(), ListNotesTool(), RememberFactTool(), SearchMemoryTool(), SummarizeContextTool(),
        ])
        print(f"✅ 已注册 {len(self.registry)} 个工具")
