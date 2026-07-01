"""
工具适配器 — 将统一 ToolCall 格式适配到外部协议 (OpenAI / MCP)。
"""
from agent.tools.adapters.openai_adapter import OpenAIAdapter

__all__ = ["OpenAIAdapter"]
