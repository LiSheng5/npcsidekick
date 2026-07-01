"""
Skill — 组合工具抽象。

Skill 是 ToolProtocol 的一个子类，表示一个"复合工具"：它在内部将任务
拆解为多个原子 ToolCall，分别调度执行，然后汇总结果。

与原子工具的区别:
  - 原子工具 (ReadFileTool): execute() 直接执行一个操作
  - Skill (AnalyzeCodeSkill): execute() → decompose → dispatch → synthesize

对 ToolRouter 来说，Skill 就是一个普通的 ToolProtocol — dispatch() 调 execute()，
Router 不需要知道内部是原子还是复合。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus


class Skill(ToolProtocol):
    """
    组合工具基类。

    子类需要:
      1. 定义 schema (与普通工具相同)
      2. 实现 decompose(task, call) → List[ToolCall]
      3. (可选) 覆盖 synthesize(results, call) → ToolResult

    使用前必须调用 set_dispatcher() 注入调度函数。

    示例:
      skill = AnalyzeCodeSkill()
      skill.set_dispatcher(router.dispatch)  # 注入 ToolRouter.dispatch
      result = skill.execute(ToolCall(tool="analyze_code", input={"path": "main.py"}))
    """

    def __init__(self):
        self._dispatcher: Optional[Callable[[ToolCall], ToolResult]] = None

    # ── 子类必须实现 ──────────────────────────────────────

    def decompose(self, task: str, call: ToolCall) -> List[ToolCall]:
        """
        将高层任务拆解为多个原子 ToolCall。

        Args:
          task: 从 call.input 中提取的任务描述
          call: 原始 ToolCall (可访问完整 input)

        Returns:
          有序的子 ToolCall 列表 (支持依赖关系)
        """
        raise NotImplementedError("子类必须实现 decompose 方法")

    def synthesize(self, results: List[ToolResult], call: ToolCall) -> ToolResult:
        """
        将多个子结果汇总为单个 ToolResult。

        默认实现: 如果全部成功，合并 data；如果有失败，标记为 ERROR。

        子类可以覆盖此方法以提供更智能的汇总 (例如用 LLM 生成摘要)。
        """
        if not results:
            return ToolResult(
                call_id=call.call_id,
                tool=self.name,
                status=ToolResultStatus.SUCCESS,
                data={"summary": "无子任务"},
            )

        all_ok = all(r.ok for r in results)
        combined_data = {
            "sub_results": [r.to_dict() for r in results],
            "success_count": sum(1 for r in results if r.ok),
            "failure_count": sum(1 for r in results if not r.ok),
        }

        # 提取第一个成功结果的关键数据
        for r in results:
            if r.ok and r.data:
                if isinstance(r.data, dict):
                    combined_data.update({f"_{r.tool}_{k}": v for k, v in r.data.items()})
                else:
                    combined_data[f"_{r.tool}_output"] = r.data

        if all_ok:
            return ToolResult(
                call_id=call.call_id,
                tool=self.name,
                status=ToolResultStatus.SUCCESS,
                data=combined_data,
            )
        else:
            errors = [r.error for r in results if not r.ok]
            return ToolResult(
                call_id=call.call_id,
                tool=self.name,
                status=ToolResultStatus.ERROR,
                data=combined_data,
                error="; ".join(errors),
            )

    # ── 调度器注入 ────────────────────────────────────────

    def set_dispatcher(self, dispatcher: Callable[[ToolCall], ToolResult]) -> None:
        """
        注入调度函数 — 通常传入 ToolRouter.dispatch。

        设计理由: Skill 不直接 import ToolRouter，避免循环依赖。
        调度器由 Orchestrator 在初始化时注入。
        """
        self._dispatcher = dispatcher

    # ── ToolProtocol 实现 ─────────────────────────────────

    def execute(self, call: ToolCall) -> ToolResult:
        """
        执行 Skill: decompose → dispatch → synthesize。

        ToolRouter 调用此方法 — 它不知道内部是复合的。
        """
        if self._dispatcher is None:
            raise RuntimeError(
                f"Skill '{self.name}' 的 dispatcher 未注入。"
                "请先调用 skill.set_dispatcher(router.dispatch)。"
            )

        task = call.input.get("task", call.input.get("description", ""))
        sub_calls = self.decompose(task, call)

        results: List[ToolResult] = []
        for sub_call in sub_calls:
            # 继承父 call 的部分属性
            if not sub_call.call_id:
                sub_call.call_id = f"{call.call_id}_sub{len(results)}"
            try:
                result = self._dispatcher(sub_call)
            except Exception as e:
                result = ToolResult(
                    call_id=sub_call.call_id,
                    tool=sub_call.tool,
                    status=ToolResultStatus.ERROR,
                    error=f"Skill 子调用异常: {e}",
                )
            results.append(result)

        return self.synthesize(results, call)


# ══════════════════════════════════════════════════════════════
# 内置 Skill 示例
# ══════════════════════════════════════════════════════════════


class AnalyzeCodeSkill(Skill):
    """
    代码分析 Skill — 读取文件 → 静态检查 → 汇总。

    用法:
      skill = AnalyzeCodeSkill()
      skill.set_dispatcher(router.dispatch)
      result = skill.execute(ToolCall(
          tool="analyze_code",
          input={"path": "main.py", "task": "分析代码质量"},
      ))
    """

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="analyze_code",
            description="分析代码文件: 读取内容并运行静态检查，返回质量报告",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要分析的代码文件路径",
                    },
                    "task": {
                        "type": "string",
                        "description": "分析任务描述 (可选)",
                    },
                },
                "required": ["path"],
            },
            category="code",
            tags=["code", "analysis", "skill"],
            is_readonly=True,
            estimated_duration_ms=5000,
        )

    def decompose(self, task: str, call: ToolCall) -> List[ToolCall]:
        path = call.input.get("path", "")
        sub_calls = [
            ToolCall(
                tool="read_file",
                input={"path": path},
                reason=f"读取 {path} 以进行代码分析",
            ),
            ToolCall(
                tool="lint_code",
                input={"path": path},
                reason=f"对 {path} 运行静态检查",
            ),
        ]
        return sub_calls

    def synthesize(self, results: List[ToolResult], call: ToolCall) -> ToolResult:
        """汇总: 将读取内容和 lint 结果合并为分析报告。"""
        base = super().synthesize(results, call)
        if base.ok:
            file_content = ""
            lint_issues = []
            for r in results:
                if r.tool == "read_file" and r.ok:
                    file_content = r.data.get("content", "") if isinstance(r.data, dict) else str(r.data)
                if r.tool == "lint_code":
                    if r.ok and isinstance(r.data, dict):
                        lint_issues = r.data.get("issues", [])
                    elif not r.ok:
                        lint_issues = [{"error": r.error}]

            base.data["analysis"] = {
                "file_size_chars": len(file_content),
                "lint_issues_count": len(lint_issues),
                "lint_issues": lint_issues,
                "verdict": "pass" if len(lint_issues) == 0 else "issues_found",
            }
        return base
