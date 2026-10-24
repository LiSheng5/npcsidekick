"""
ProviderProtocol — LLM 提供商的统一抽象（抽象基类；名字里的 "Protocol" 是历史沿革，
实际不是 `typing.Protocol`）。

所有 Provider 实现此基类，LLMClient 通过委托调用，不关心底层是
OpenAI、DeepSeek 还是任何 OpenAI 兼容 API。

设计参考:
  - LiteLLM 的 provider 抽象: 统一接口, 每个 provider 一个实现
  - OpenAI SDK: chat() + stream() 两套 API
  - 保持小接口: chat + stream + model_name，另有可选的 replays_reasoning_content
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, List, Optional

from core.types import LLMResponse, StreamChunk   # 数据契约在 types.py，抽象层不再反向依赖实现层


class ProviderProtocol(ABC):
    """
    LLM Provider 接口（抽象基类，不是 typing.Protocol）。

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

    @property
    def replays_reasoning_content(self) -> bool:
        """带 tools 时，是否必须把 assistant 的 reasoning_content 原样回传。

        DeepSeek 官方硬要求（缺了直接 400）；其它家没有这个字段，默认 False ——
        别把未知字段塞给不认它的端点。
        """
        return False

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
          reasoning_effort: 思考档位（None = 不指定；off/disabled/none = 显式关闭；
            minimal/low/medium/high/xhigh/max = 开启，按端点方言归一），
            以具名参数传给 Provider（思考参数一律请求体顶层，见 openai_provider._thinking_params）

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
          reasoning_effort: 思考档位（None = 不指定；off/disabled/none = 显式关闭；
            minimal/low/medium/high/xhigh/max = 开启，按端点方言归一），
            以具名参数传给 Provider（思考参数一律请求体顶层，见 openai_provider._thinking_params）

        Yields:
          StreamChunk: content / tool_call_delta / finish_reason
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.model_name}>"
