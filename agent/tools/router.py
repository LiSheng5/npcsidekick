"""
Tool Router — 统一工具路由层。

职责:
  1. 接收 ToolCall，分发到正确的工具
  2. 执行前验证 (validate) + 安全检查
  3. 执行后收集结果，统一为 ToolResult
  4. 支持超时、重试 (由上层 Executor 调用)
  5. 动态工具选择: 根据 task context 推荐工具

这是所有工具调用的唯一入口。
"""
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional, Any

from agent.tools.schema import (
    ToolProtocol, ToolCall, ToolResult, ToolResultStatus, ToolSchema
)
from agent.tools.registry import ToolRegistry, get_registry


class ToolRouter:
    """
    工具路由器 — 每个 Agent 实例创建一个。

    使用方式:
      router = ToolRouter(registry)
      result = router.dispatch(ToolCall(tool="read_file", input={"path": "x.py"}))
    """

    def __init__(self, registry: Optional[ToolRegistry] = None):
        self.registry = registry or get_registry()
        self._call_history: List[ToolResult] = []

    # ── 核心: 分发工具调用 ────────────────────────────

    def dispatch(self, call: ToolCall) -> ToolResult:
        """
        分发一个工具调用到相应的处理器。

        流程:
          1. 分配 call_id (如果没有)
          2. 验证工具存在
          3. 执行前置验证 (validate)
          4. 执行工具
          5. 记录并返回 ToolResult
        """
        if not call.call_id:
            call.call_id = f"tc_{uuid.uuid4().hex[:12]}"

        start = time.time()

        # 1. 查找工具
        error = self.registry.validate_call(call.tool)
        if error:
            result = ToolResult(
                call_id=call.call_id,
                tool=call.tool,
                status=ToolResultStatus.ERROR,
                error=error,
            )
            self._record(result, start)
            return result

        tool = self.registry.get(call.tool)

        # 2. 前置验证
        passed, reason = tool.validate(call)
        if not passed:
            result = ToolResult(
                call_id=call.call_id,
                tool=call.tool,
                status=ToolResultStatus.REJECTED,
                error=reason,
            )
            self._record(result, start)
            return result

        # 3. 执行
        try:
            result = tool.execute(call)
            result.call_id = call.call_id
            result.tool = call.tool
        except Exception as exc:
            result = ToolResult(
                call_id=call.call_id,
                tool=call.tool,
                status=ToolResultStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )

        self._record(result, start)
        return result

    def dispatch_many(self, calls: List[ToolCall]) -> List[ToolResult]:
        """
        分发多个工具调用（串行，按依赖顺序）。
        对于独立调用，上层可用 ThreadPoolExecutor 并行。
        """
        results = []
        for call in calls:
            result = self.dispatch(call)
            results.append(result)
        return results

    # ── 动态工具选择 ──────────────────────────────────

    def recommend_tools(
        self,
        task_description: str,
        category: Optional[str] = None,
        max_results: int = 5,
    ) -> List[ToolSchema]:
        """
        根据任务描述推荐合适的工具。

        策略:
          1. 关键词匹配名称、描述、标签
          2. 按类别过滤
          3. 返回最相关的 top-N
        """
        if category:
            candidates = self.registry.find_by_category(category)
        else:
            candidates = self.registry.list_all()

        # 简单相关度评分: 关键词命中数
        keywords = set(task_description.lower().split())
        scored = []
        for tool in candidates:
            s = tool.schema
            text = f"{s.name} {s.description} {' '.join(s.tags)}".lower()
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                scored.append((score, s))

        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:max_results]] if scored else [
            t.schema for t in candidates[:max_results]
        ]

    def recommend_for_step(self, step_description: str) -> List[str]:
        """返回推荐的工具名称列表。"""
        schemas = self.recommend_tools(step_description)
        return [s.name for s in schemas]

    # ── 查询 ──────────────────────────────────────────

    @property
    def call_history(self) -> List[ToolResult]:
        return list(self._call_history)

    def last_result(self) -> Optional[ToolResult]:
        return self._call_history[-1] if self._call_history else None

    def total_calls(self) -> int:
        return len(self._call_history)

    # ── internal ──────────────────────────────────────

    def _record(self, result: ToolResult, start_time: float) -> None:
        result.duration_ms = (time.time() - start_time) * 1000
        self._call_history.append(result)
