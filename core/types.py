"""
LLM 数据类型 — 请求/响应管道的数据契约。

StreamChunk: LLM 流式响应的最小单位（provider → client → turn）
LLMResponse: LLM 非流式响应的封装（provider → client / 摘要等调用方）

放一起的理由：两者同属跨层数据契约；此前 LLMResponse 长在实现层
（core/client.py）里、抽象层（core/base.py）还要反向 import 它，层次是拧的。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class StreamChunk:
    """
    LLM 流式响应的单个 chunk。

    对应 OpenAI streaming API 的一个 delta:
      - content: 文本增量（空串 = 无文本，看 has_content）
      - reasoning: 思考增量 (DeepSeek: reasoning_content / OpenRouter: reasoning)
      - tool_call_delta: 工具调用增量 (None 表示无工具调用)
      - finish_reason: 终止原因 (最后一个 chunk 才有值)

    使用示例:
      async for chunk in client.stream(messages):
          if chunk.has_content:
              print(chunk.content, end="", flush=True)
          if chunk.has_tool_call:
              ...
    """
    content: str = ""
    reasoning: str = ""              # 思考增量；带 tools 时 DeepSeek 要求原样回传
    tool_call_delta: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    model: str = ""
    index: int = 0  # chunk 序号

    @property
    def has_content(self) -> bool:
        return bool(self.content)

    @property
    def has_tool_call(self) -> bool:
        return self.tool_call_delta is not None

    @property
    def has_reasoning(self) -> bool:
        return bool(self.reasoning)

    def __repr__(self) -> str:
        parts = []
        if self.has_content:
            parts.append(f"content={self.content[:40]}...")
        if self.has_reasoning:
            parts.append("reasoning=...")
        if self.has_tool_call:
            parts.append("tool_delta=...")
        if self.finish_reason:
            parts.append(f"finish={self.finish_reason}")
        return f"<StreamChunk [{self.index}] {' '.join(parts) or 'empty'}>"


class LLMResponse:
    """
    LLM 响应封装。

    属性:
      content: 文本内容
      tool_calls: OpenAI tool_call 对象列表
      finish_reason: "stop" | "tool_calls" | "length"
      model: 使用的模型名
      usage: token 用量统计
    """

    def __init__(self, content: str, tool_calls: List, finish_reason: str,
                 model: str, usage: Any, reasoning: str = ""):
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.model = model
        self.usage = usage
        self.reasoning = reasoning   # 思考可视化: 模型思考内容(无则空串)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def is_stop(self) -> bool:
        return self.finish_reason == "stop"

    def __repr__(self) -> str:
        tc = f", {len(self.tool_calls)} tool_calls" if self.tool_calls else ""
        return f"<LLMResponse finish={self.finish_reason}{tc} content={self.content[:60]}...>"
