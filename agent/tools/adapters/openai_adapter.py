"""
OpenAI function-calling 适配器。

职责:
  - 将内部 ToolSchema 转换为 OpenAI tools 数组
  - 将 OpenAI tool_call 响应转换回内部 ToolCall
  - 将 ToolResult 转换为 OpenAI tool message 格式
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from agent.tools.schema import ToolCall, ToolResult, ToolResultStatus
from agent.tools.registry import ToolRegistry


class OpenAIAdapter:
    """
    管理内部工具格式 ↔ OpenAI function-calling 格式的双向转换。

    使用方式:
      adapter = OpenAIAdapter(registry)
      openai_tools = adapter.to_openai_tools()
      internal_call = adapter.from_openai_response(openai_tool_call)
      openai_message = adapter.result_to_message(internal_result)
    """

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def to_openai_tools(self) -> List[dict]:
        """导出所有注册工具为 OpenAI tools 数组。"""
        return [t.schema.to_openai_function() for t in self.registry]

    def to_openai_tools_filtered(self, allowed_names: List[str]) -> List[dict]:
        """导出指定工具子集。"""
        return [
            self.registry.get(name).schema.to_openai_function()
            for name in allowed_names
            if self.registry.has(name)
        ]

    def from_openai_response(self, openai_call) -> ToolCall:
        """
        将 OpenAI 的 tool_call 对象转换为内部 ToolCall。
        """
        try:
            arguments = json.loads(openai_call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}

        return ToolCall(
            tool=openai_call.function.name,
            input=arguments,
            call_id=getattr(openai_call, "id", ""),
        )

    def result_to_message(self, result: ToolResult) -> dict:
        """
        将 ToolResult 转换为 OpenAI tool message 格式。
        """
        if result.ok:
            content = json.dumps({"status": "success", "data": result.data}, ensure_ascii=False)
        elif result.status == ToolResultStatus.TIMEOUT:
            content = json.dumps({"status": "timeout", "error": result.error}, ensure_ascii=False)
        else:
            content = json.dumps({"status": "error", "error": result.error}, ensure_ascii=False)

        return {
            "role": "tool",
            "tool_call_id": result.call_id,
            "name": result.tool,
            "content": content,
        }

    def results_to_messages(self, results: List[ToolResult]) -> List[dict]:
        """批量转换结果。"""
        return [self.result_to_message(r) for r in results]
