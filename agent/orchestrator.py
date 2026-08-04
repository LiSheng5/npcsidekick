"""
AgentOrchestrator — 主编排器，连接所有 4 层。

流程:
  User Input → Planner.plan() → TaskPlan
    → [Loop] Executor.execute_step() → Reflector.reflect()
    → continue / retry / replan / stop / ask_user
    → synthesize answer → return to User

run_stream() 在此基础上增加 AsyncGenerator[StreamEvent]，实时产出进度事件。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Callable

from agent.planner.planner import Planner
from agent.planner.task_plan import TaskPlan, Step, StepStatus
from agent.planner.reflector import Reflector, ReflectionDecision
from agent.executor.executor import Executor
from agent.executor.step_context import StepContext
from agent.tools.router import ToolRouter, ApprovalHandler
from agent.tools.registry import ToolRegistry, get_registry
from agent.tools.adapters.openai_adapter import OpenAIAdapter
from agent.tools.schema import ToolCall, ToolResult, ToolResultStatus
from agent.tools.skill import Skill, AnalyzeCodeSkill
from agent.memory.memory_manager import MemoryManager
from agent.llm.client import LLMClient
from agent.llm.types import StreamEvent, StreamEventType
from agent.logging_config import log as _log
import config


class AgentOrchestrator:
    """主编排器 — Agent 系统的入口。"""

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
        self.router = ToolRouter(self.registry, approval_handler=self._approve)
        self.adapter = OpenAIAdapter(self.registry)

        # 注入 MemoryManager 到记忆工具（使它们走向量索引而非直接文件 I/O）
        from agent.tools.builtin.memory_tools import set_memory_manager
        set_memory_manager(self.memory)

        # 设置文件工具 workspace 根目录（P0-2：防止路径穿越读/写任意文件）
        from agent.tools.builtin.file_tools import set_workspace_root
        from agent.settings import get_settings
        set_workspace_root(get_settings().base_dir)

        # 注册内置工具
        self._register_builtin_tools()

        # 注入 Skill dispatchers — run() 与 run_stream() 共用同一注入点
        self._inject_skill_dispatchers()

        self.planner = Planner(self.llm, self.memory, self.registry)
        self.executor = Executor(self.llm, self.router, self.registry)
        self.reflector = Reflector(self.llm)

    # ── 审批门控（CLI）─────────────────────────────────

    @staticmethod
    def _approve(call, schema) -> bool:
        """CLI 审批处理器:打印工具名 + 参数 → 等待用户 Y/n。"""
        import json

        print(f"\n{'='*60}")
        print(f"  ⚠  危险操作需要确认")
        print(f"  工具: {schema.name}")
        print(f"  描述: {schema.description}")
        print(f"  参数: {json.dumps(call.input, ensure_ascii=False, indent=2)}")
        print(f"{'='*60}")
        while True:
            ans = input("  批准执行? [y/N]: ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("", "n", "no"):
                return False
            print("  请输入 y (批准) 或 n (拒绝)")

    # ── 主循环 ──────────────────────────────────────────

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
        log = _log.bind(phase="plan")
        log.info("planning_started", task=user_input[:80])

        plan = self.planner.plan(user_input)
        self.current_plan = plan
        log.info("plan_generated", goal=plan.goal, steps_count=len(plan.steps),
                 estimated_tools=plan.estimated_tools)
        for s in plan.steps:
            log.debug("step_detail", step_id=s.step_id, description=s.description[:80],
                      tool=s.tool, depends_on=s.depends_on)

        # ── Phase 2: EXECUTE ───────────────────────────
        log = _log.bind(phase="execute")
        log.info("execution_started", total_steps=len(plan.steps))

        previous_results: Dict[int, Any] = {}
        max_iterations = config.MAX_PLAN_STEPS * 2  # 安全上限
        iteration = 0

        stopped = False
        while not plan.is_complete() and not stopped and iteration < max_iterations:
            iteration += 1

            # 获取就绪步骤
            ready = plan.get_ready_steps()
            if not ready:
                # 可能所有步骤都失败了
                failed = plan.get_failed_steps()
                if failed:
                    log.warning("no_ready_steps", failed_count=len(failed))
                    decision = self.reflector.reflect_plan(plan, failed)
                    if decision == ReflectionDecision.REPLAN:
                        log.info("replan_triggered_by_reflect_plan")
                        plan = self.planner.replan(plan, failed[0], "步骤失败，重新规划")
                        self.current_plan = plan
                        continue
                    elif decision == ReflectionDecision.ASK_USER:
                        return self._synthesize(plan, "部分步骤执行失败，需要您的指示才能继续。")
                    elif decision == ReflectionDecision.STOP:
                        stopped = True
                        break
                else:
                    break  # 全部完成

            for step in ready:
                # 回调
                if self.on_step_start:
                    self.on_step_start(step)

                log.info("step_start", step_id=step.step_id,
                         description=step.description[:80], retry_count=step.retry_count)
                plan.current_step = step.step_id

                # 执行步骤 (包含重试循环)
                updated_step, result = self._execute_single_step(step, plan, previous_results)

                if self.on_step_complete:
                    self.on_step_complete(updated_step, result)

                # 更新结果
                if updated_step.is_success:
                    previous_results[updated_step.step_id] = updated_step.result
                    plan.total_steps_completed += 1
                    log.info("step_success", step_id=step.step_id,
                             duration_ms=round(updated_step.duration_ms or 0))
                else:
                    plan.total_steps_failed += 1
                    log.error("step_failed", step_id=step.step_id,
                              error=updated_step.error, retry_count=updated_step.retry_count)

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
                    log.warning("replan_triggered", step_id=step.step_id,
                                error=updated_step.error)
                    plan = self.planner.replan(plan, updated_step, updated_step.error or "未知错误")
                    self.current_plan = plan
                    break  # 退出 ready 循环，重新获取就绪步骤

                elif decision == ReflectionDecision.ASK_USER:
                    return self._synthesize(
                        plan,
                        f"我需要更多信息才能继续。{step.description} 失败了: {updated_step.error}",
                    )

                elif decision == ReflectionDecision.STOP:
                    stopped = True
                    break

        # ── Phase 3: SYNTHESIZE ────────────────────────
        answer = self._synthesize(plan, None)
        duration = time.time() - start_time

        self._finalize_execution(user_input, plan, answer, duration)
        return answer

    def run_chat(self, user_input: str) -> str:
        """
        Chat 模式 — 统一走 Agent Loop，由 Planner 在看到完整上下文 (工具列表 + 记忆)
        后自行判断任务复杂度：简单问题生成 1 步、复杂问题分解多步。

        只对纯闲聊问候保留快速通道，跳过 Plan→Execute→Reflect 循环。
        """
        simple_greetings = {
            "你好", "hi", "hello", "嘿", "嗨", "在吗",
            "谢谢", "thanks", "thank you",
            "bye", "再见", "exit", "退出", "goodbye",
        }
        stripped = user_input.lower().strip().lstrip('﻿')
        if stripped in simple_greetings:
            if self.memory is None:
                return "你好！请先初始化 Agent (调用 initialize())。"
            self.memory.add_message("user", user_input)
            answer = "你好！有什么我可以帮你的吗？"
            self.memory.add_message("assistant", answer)
            self.memory.save()
            return answer

        return self.run(user_input)

    # ── Streaming ──────────────────────────────────────

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[StreamEvent, None]:
        """流式运行 Agent 循环 — 实时产出进度事件。"""
        if not self.llm:
            raise RuntimeError("Agent 未初始化。请先调用 initialize()。")

        start_time = time.time()

        # 注入 Skill dispatchers (确保 Skills 可以调 Router)
        self._inject_skill_dispatchers()

        # 记录用户输入
        self.memory.add_message("user", user_input)

        # ── Phase 1: PLAN ──────────────────────────────
        yield StreamEvent.thinking("正在分析任务...")

        log = _log.bind(phase="plan")
        log.info("planning_started", task=user_input[:80])

        plan = self.planner.plan(user_input)
        self.current_plan = plan

        log.info("plan_generated", goal=plan.goal, steps_count=len(plan.steps))
        yield StreamEvent.plan_ready(
            goal=plan.goal,
            steps_count=len(plan.steps),
            estimated_tools=plan.estimated_tools,
        )

        # ── Phase 2: EXECUTE ───────────────────────────
        log = _log.bind(phase="execute")
        log.info("execution_started", total_steps=len(plan.steps))

        previous_results: Dict[int, Any] = {}
        max_iterations = config.MAX_PLAN_STEPS * 2
        iteration = 0

        stopped = False
        while not plan.is_complete() and not stopped and iteration < max_iterations:
            iteration += 1

            ready = plan.get_ready_steps()
            if not ready:
                failed = plan.get_failed_steps()
                if failed:
                    log.warning("no_ready_steps", failed_count=len(failed))
                    yield StreamEvent.reflection(
                        decision="replan",
                        reason=f"{len(failed)} 个步骤失败，需要重新规划",
                    )
                    decision = self.reflector.reflect_plan(plan, failed)
                    if decision == ReflectionDecision.REPLAN:
                        log.info("replan_triggered_by_reflect_plan")
                        plan = self.planner.replan(plan, failed[0], "步骤失败，重新规划")
                        self.current_plan = plan
                        yield StreamEvent.plan_ready(
                            goal=plan.goal,
                            steps_count=len(plan.steps),
                        )
                        continue
                    elif decision == ReflectionDecision.ASK_USER:
                        yield StreamEvent.error(
                            message="部分步骤执行失败，需要您的指示才能继续。",
                        )
                        answer = self._synthesize(plan, "部分步骤执行失败，需要您的指示才能继续。")
                        duration = time.time() - start_time
                        self._finalize_execution(user_input, plan, answer, duration)
                        yield StreamEvent.done(answer=answer, duration_sec=duration)
                        return
                    elif decision == ReflectionDecision.STOP:
                        stopped = True
                        break
                else:
                    break

            for step in ready:
                yield StreamEvent.step_start(
                    step_id=step.step_id,
                    description=step.description,
                    tool=step.tool,
                )

                if self.on_step_start:
                    self.on_step_start(step)

                log.info("step_start", step_id=step.step_id,
                         description=step.description[:80])
                plan.current_step = step.step_id

                # 执行步骤
                updated_step, result = self._execute_single_step(step, plan, previous_results)

                yield StreamEvent.tool_result(
                    tool_name=result.tool,
                    ok=result.ok,
                    step_id=step.step_id,
                )

                if self.on_step_complete:
                    self.on_step_complete(updated_step, result)

                if updated_step.is_success:
                    previous_results[updated_step.step_id] = updated_step.result
                    plan.total_steps_completed += 1
                    log.info("step_success", step_id=step.step_id)
                else:
                    plan.total_steps_failed += 1
                    log.error("step_failed", step_id=step.step_id,
                              error=updated_step.error)

                yield StreamEvent.step_done(
                    step_id=step.step_id,
                    success=updated_step.is_success,
                )

                # ── REFLECT ─────────────────────────────
                decision = self.reflector.reflect(updated_step, result, plan)
                yield StreamEvent.reflection(
                    decision=decision.value,
                    reason=f"步骤 {step.step_id} 状态: {updated_step.status.value}",
                )

                if decision == ReflectionDecision.CONTINUE:
                    continue

                elif decision == ReflectionDecision.RETRY:
                    step.status = StepStatus.PENDING
                    continue

                elif decision == ReflectionDecision.REPLAN:
                    log.warning("replan_triggered", step_id=step.step_id)
                    plan = self.planner.replan(plan, updated_step, updated_step.error or "未知错误")
                    self.current_plan = plan
                    yield StreamEvent.plan_ready(
                        goal=plan.goal,
                        steps_count=len(plan.steps),
                    )
                    break

                elif decision == ReflectionDecision.ASK_USER:
                    yield StreamEvent.error(
                        message=f"需要更多信息: {step.description} 失败 — {updated_step.error}",
                    )
                    answer = self._synthesize(
                        plan,
                        f"我需要更多信息才能继续。{step.description} 失败了: {updated_step.error}",
                    )
                    duration = time.time() - start_time
                    self._finalize_execution(user_input, plan, answer, duration)
                    yield StreamEvent.done(answer=answer, duration_sec=duration)
                    return

                elif decision == ReflectionDecision.STOP:
                    stopped = True
                    break

        # ── Phase 3: SYNTHESIZE ────────────────────────
        yield StreamEvent(type=StreamEventType.SYNTHESIS, message="正在生成最终答案...")

        # 构建 LLM 消息 (或获取直接答案)
        messages, direct_answer = self._build_synthesis_prompt(plan)

        if messages is not None and self.llm:
            # 真正流式: 逐 token 从 LLM stream 读取并 yield text_delta
            full_answer = ""
            try:
                async for chunk in self.llm.stream(messages):
                    if chunk.has_content:
                        yield StreamEvent.text_delta(chunk.content)
                        full_answer += chunk.content
            except Exception:
                _log.exception("synthesis_stream_failed")
            if full_answer.strip():
                answer = full_answer
            else:
                answer = "任务执行完成。请查看上述步骤结果。"
        else:
            answer = direct_answer or "任务执行完成。"

        duration = time.time() - start_time

        # 记录到记忆
        self._finalize_execution(user_input, plan, answer, duration)

        yield StreamEvent.done(answer=answer, duration_sec=duration)

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
                results_text.append(f"Step {s.step_id} ({s.description[:60]}): [失败] {s.error}")

        if not results_text:
            return "任务已完成，但没有产生具体结果。"

        results_combined = "\n".join(results_text)

        # 如果只有 1 步且成功，智能提取结果
        if len(plan.steps) == 1 and plan.steps[0].is_success:
            result = plan.steps[0].result
            if isinstance(result, dict):
                # 优先提取自然语言字段
                for key in ["response", "content", "answer", "summary"]:
                    if key in result:
                        return str(result[key])
                # 常见工具结果 → 自然语言
                if "time" in result:
                    return f'当前时间: {result["time"]}'
                if "result" in result:
                    return str(result["result"])
                if "data" in result:
                    return str(result["data"])
                # 其他情况让 LLM 格式化为自然语言
                return self._synthesize_single_result(plan.steps[0])
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

    def _synthesize_single_result(self, step: Step) -> str:
        """用 LLM 将单个步骤结果转为自然语言。"""
        try:
            result_json = json.dumps(step.result, ensure_ascii=False)[:600]
            messages = [
                {"role": "system", "content": "将工具执行结果转换为简洁的自然语言回答。一句话说完，不要加引号或解释。"},
                {"role": "user", "content": f"步骤: {step.description}\n结果: {result_json}"},
            ]
            response = self.llm.chat(messages, max_tokens=120)
            return response.content or str(step.result)[:200]
        except Exception:
            return str(step.result)[:200]

    def _build_synthesis_prompt(self, plan: TaskPlan) -> tuple[Optional[List[dict]], Optional[str]]:
        """
        构建 LLM 合成所需的消息。如果不需要 LLM 调用则返回直接答案。

        Returns:
          (messages, direct_answer) — messages 为 None 时使用 direct_answer，
          messages 非 None 时需要调用 LLM stream。
        """
        results_text = []
        for s in plan.steps:
            if s.is_success and s.result:
                result_str = json.dumps(s.result, ensure_ascii=False)[:500]
                results_text.append(f"Step {s.step_id} ({s.description[:60]}): {result_str}")
            elif s.status == StepStatus.FAILED:
                results_text.append(f"Step {s.step_id} ({s.description[:60]}): [失败] {s.error}")

        if not results_text:
            return None, "任务已完成，但没有产生具体结果。"

        # 单步成功 → 智能提取
        if len(plan.steps) == 1 and plan.steps[0].is_success:
            result = plan.steps[0].result
            if isinstance(result, dict):
                for key in ["response", "content", "answer", "summary"]:
                    if key in result:
                        return None, str(result[key])
                if "time" in result:
                    return None, f'当前时间: {result["time"]}'
                if "result" in result:
                    return None, str(result["result"])
                if "data" in result:
                    return None, str(result["data"])
            else:
                return None, str(result)[:1000]

        # 需要 LLM 合成 → 返回消息
        results_combined = "\n".join(results_text)
        messages = [
            {"role": "system", "content": "你是一个结果总结助手。请根据以下步骤执行结果，生成一个简洁、有帮助的最终答案。用自然语言回答。"},
            {"role": "user", "content": f"原始目标: {plan.goal}\n\n执行结果:\n{results_combined}\n\n请给出最终答案。"},
        ]
        return messages, None

    def _finalize_execution(
        self,
        user_input: str,
        plan: TaskPlan,
        answer: str,
        duration: float,
    ) -> None:
        """记录执行结果到记忆和历史 (run() 和 run_stream() 共用)。"""
        self.memory.add_message("assistant", answer)

        compressed = self.memory.maybe_compress(self.llm)
        if compressed > 0:
            _log.info("context_compressed", messages_compressed=compressed)

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

        self._execution_history.append({
            "input": user_input,
            "plan": plan.to_dict(),
            "answer": answer,
            "duration_sec": duration,
            "timestamp": datetime.now().isoformat(),
        })

        _log.info("execution_complete", duration_sec=round(duration, 1),
                 steps_completed=plan.total_steps_completed,
                 steps_failed=plan.total_steps_failed,
                 steps_total=len(plan.steps))

    def _inject_skill_dispatchers(self) -> None:
        """将 ToolRouter.dispatch 注入所有已注册的 Skill。"""
        if not self.router:
            return
        for tool in self.registry:
            if isinstance(tool, Skill):
                tool.set_dispatcher(self.router.dispatch)
                _log.debug("skill_dispatcher_injected", skill_name=tool.name)

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
            # Skills (组合工具 — 内部拆解为原子调用)
            AnalyzeCodeSkill(),
        ])
        _log.info("tools_registered", count=len(self.registry))
