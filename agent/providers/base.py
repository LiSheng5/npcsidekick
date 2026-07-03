"""
ProviderProtocol — LLM 提供商的统一抽象。

所有 Provider 实现此协议，LLMClient 通过委托调用，不关心底层是
OpenAI、DeepSeek 还是任何 OpenAI 兼容 API。

设计参考:
  - LiteLLM 的 provider 抽象: 统一接口, 每个 provider 一个实现
  - OpenAI SDK: chat() + stream() 两套 API
  - 保持最小接口: 只有 chat + stream + model_name
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional

from agent.llm.client import LLMResponse
from agent.llm.types import StreamChunk


class ProviderProtocol(ABC):
    """
    LLM Provider 接口。

    每个 Provider 实现两个核心方法:
      - chat():    同步非流式调用 → LLMResponse
      - stream():  异步流式调用 → AsyncGenerator[StreamChunk, None]

    使用方式:
      provider = OpenAIProvider(api_key="...", base_url="...", model="gpt-4")
      response = provider.chat([{"role": "user", "content": "hi"}])
      async for chunk in provider.stream([{"role": "user", "content": "hi"}]):
          ...
    """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """返回当前使用的模型名。"""
        ...

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict] = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """
        同步非流式对话。

        Args:
          messages: OpenAI 格式的消息列表
          tools: OpenAI tools 数组 (可选)
          tool_choice: "auto" | "none" | "required"
          temperature: 温度参数 (可选, 使用默认值)
          max_tokens: 最大 token 数 (可选, 使用默认值)
          response_format: {"type": "json_object"} 强制 JSON 输出
          reasoning_effort: 推理力度 (None | "low" | "medium" | "high" | "max")，
            通过 extra_body 传递给 API

        Returns:
          LLMResponse: content + tool_calls + finish_reason + usage
        """
        ...

    @abstractmethod
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
        异步流式对话。

        Args:
          messages: OpenAI 格式的消息列表
          tools: OpenAI tools 数组 (可选)
          tool_choice: "auto" | "none" | "required"
          temperature: 温度参数
          max_tokens: 最大 token 数
          reasoning_effort: 推理力度 (None | "low" | "medium" | "high" | "max")，
            通过 extra_body 传递给 API

        Yields:
          StreamChunk: content / tool_call_delta / finish_reason
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.model_name}>"
