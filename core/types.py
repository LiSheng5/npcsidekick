"""
流式数据类型 — AsyncGenerator 管道的数据契约。

StreamChunk: LLM 流式响应的最小单位
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class StreamChunk:
    """
    LLM 流式响应的单个 chunk。

    对应 OpenAI streaming API 的一个 delta:
      - content: 文本增量 (None 表示无文本)
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

    def __repr__(self) -> str:
        parts = []
        if self.has_content:
            parts.append(f"content={self.content[:40]}...")
        if self.has_tool_call:
            parts.append("tool_delta=...")
        if self.finish_reason:
            parts.append(f"finish={self.finish_reason}")
        return f"<StreamChunk [{self.index}] {' '.join(parts) or 'empty'}>"
