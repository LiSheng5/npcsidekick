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

import time
from typing import Any, AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from agent.llm.types import StreamChunk
from agent.logging_config import log
import config

if TYPE_CHECKING:
    from agent.providers.base import ProviderProtocol

# ── 网关抖动重试（2026-08-25, 思想借自 ExponentialBackoff/DSH retryPolicy）──
# 开关: NPC_LLM_RETRY=1 才启用（代码默认关 — 家规）。每次调用现读环境变量，可热切。
# 节奏: 默认最多补试 2 次，等待 2s/6s（最坏多花 8s，仍在 talk 60s 预算内）。
#       NPC_LLM_RETRY_DELAYS="3,9" 可自定义。
# 只救瞬时病（超时/连接断/限流429/5xx），参数错(4xx)立刻原样抛出——盲试是浪费预算。


def _retry_delays() -> List[float]:
    import os
    raw = os.environ.get("NPC_LLM_RETRY_DELAYS", "").strip()
    if not raw:
        return [2.0, 6.0]
    try:
        return [max(0.0, float(x)) for x in raw.split(",") if x.strip()]
    except ValueError:
        return [2.0, 6.0]


def _is_retryable(exc: BaseException) -> bool:
    """不依赖具体 SDK 的宽判别: 看异常名和 status_code 属性。"""
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "ratelimit",
                               "internal", "overloaded", "unavailable")):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _chat_with_retry(provider: "ProviderProtocol", kwargs: Dict[str, Any]) -> "LLMResponse":
    delays = _retry_delays() if __import__("os").environ.get("NPC_LLM_RETRY") == "1" else []
    attempt = 0
    while True:
        try:
            return provider.chat(**kwargs)
        except Exception as exc:
            if attempt >= len(delays) or not _is_retryable(exc):
                raise
            wait = delays[attempt]
            attempt += 1
            log.warning("llm_retry_scheduled", attempt=attempt,
                        delay_s=wait, error=str(exc)[:120])
            time.sleep(wait)


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
        return _chat_with_retry(self._provider, dict(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        ))

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

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "tool_calls_count": len(self.tool_calls),
            "finish_reason": self.finish_reason,
            "model": self.model,
            "reasoning": self.reasoning,
        }

    def __repr__(self) -> str:
        tc = f", {len(self.tool_calls)} tool_calls" if self.tool_calls else ""
        return f"<LLMResponse finish={self.finish_reason}{tc} content={self.content[:60]}...>"
