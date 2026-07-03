"""
LLM Client — 统一的 LLM 调用接口 (薄封装)。

LLMClient 本身不直接调用 API — 它委托给 ProviderProtocol 实现。
这样做的好处:
  - 切换 Provider (OpenAI → DeepSeek) 不需要改 LLMClient
  - 测试可注入 Mock Provider，不需要 patch OpenAI 库
  - 添加新 Provider 只需实现 ProviderProtocol

支持:
  - OpenAI 兼容 API (DeepSeek / OpenAI / 任何兼容服务)
  - 工具调用 (function calling)
  - 流式和非流式
  - 流式输出 (async generator)
  - 结构化输出 (JSON mode)
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from agent.llm.types import StreamChunk
import config

if TYPE_CHECKING:
    from agent.providers.base import ProviderProtocol


class LLMClient:
    """
    统一的 LLM 客户端 — ProviderProtocol 的薄封装。

    使用方式:
      # 生产 — 自动选择 Provider
      client = LLMClient()
      response = client.chat(messages, tools=[...])

      # 测试 — 注入 Mock Provider
      mock_provider = MagicMock(spec=ProviderProtocol)
      client = LLMClient(provider=mock_provider)

      # 流式
      async for chunk in client.stream(messages):
          print(chunk.content, end="")
    """

    def __init__(self, provider: Optional["ProviderProtocol"] = None):
        """
        Args:
          provider: 可选的 Provider 实例 (用于测试注入)。
                    如果不提供，则通过工厂自动创建。
        """
        if provider is not None:
            self._provider = provider
        else:
            from agent.providers.factory import create_provider

            api_key = config.API_KEY
            if not api_key:
                raise RuntimeError(
                    "请设置 API_KEY (在 config.py) 或环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY"
                )
            self._provider = create_provider(
                api_key=api_key,
                model_name=config.MODEL_NAME,
                base_url=config.BASE_URL,
                temperature=config.TEMPERATURE,
                max_tokens=config.MAX_TOKENS,
            )

        # 从 provider 同步属性 (向后兼容)
        self.model = self._provider.model_name
        self.temperature = getattr(self._provider, '_temperature', config.TEMPERATURE)
        self.max_tokens = getattr(self._provider, '_max_tokens', config.MAX_TOKENS)

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict] = None,
        reasoning_effort: str | None = None,
    ) -> "LLMResponse":
        """
        发送对话请求，返回 LLMResponse。

        Args:
          messages: OpenAI 格式的消息列表
          tools: OpenAI tools 数组 (可选)
          tool_choice: "auto" | "none" | "required"
          temperature: 覆盖默认温度
          max_tokens: 覆盖默认 max_tokens
          response_format: {"type": "json_object"} 强制 JSON 输出
          reasoning_effort: 推理力度 (None | "low" | "medium" | "high" | "max")
        """
        return self._provider.chat(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )

    def chat_with_structured_output(
        self,
        messages: List[Dict[str, str]],
        tools: List[dict],
    ) -> "LLMResponse":
        """
        强制返回工具调用（或文本）。
        用于 Planner 生成结构化 TaskPlan。
        """
        return self.chat(messages, tools=tools, tool_choice="auto")

    # ── Streaming ────────────────────────────────────────

    async def stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: str | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        流式 LLM 调用 — 返回 AsyncGenerator[StreamChunk]。

        使用方式:
          async for chunk in client.stream(messages):
              if chunk.has_content:
                  print(chunk.content, end="", flush=True)
              if chunk.has_tool_call:
                  ...

        Args:
          messages: OpenAI 格式的消息列表
          tools: OpenAI tools 数组 (可选)
          tool_choice: "auto" | "none" | "required"
          temperature: 覆盖默认温度
          max_tokens: 覆盖默认 max_tokens
          reasoning_effort: 推理力度 (None | "low" | "medium" | "high" | "max")
        """
        async for chunk in self._provider.stream(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        ):
            yield chunk

    @property
    def provider(self) -> "ProviderProtocol":
        """获取底层 Provider (调试用)。"""
        return self._provider


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
                 model: str, usage: Any):
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.model = model
        self.usage = usage

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def is_stop(self) -> bool:
        return self.finish_reason == "stop"

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "tool_calls_count": len(self.tool_calls),
            "finish_reason": self.finish_reason,
            "model": self.model,
        }

    def __repr__(self) -> str:
        tc = f", {len(self.tool_calls)} tool_calls" if self.tool_calls else ""
        return f"<LLMResponse finish={self.finish_reason}{tc} content={self.content[:60]}...>"
