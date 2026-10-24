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

import asyncio
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, TYPE_CHECKING

from core.config_flags import env_text
from core.types import LLMResponse, StreamChunk   # LLMResponse 的家在 types.py，这里 re-export
from core.logging_config import log
from core import config

if TYPE_CHECKING:
    from core.base import ProviderProtocol

# ── 网关抖动重试（2026-08-25, 思想借自 ExponentialBackoff/DSH retryPolicy）──
# 开关: NPC_LLM_RETRY=1 才启用（代码默认关 — 家规）。每次调用现读环境变量，可热切。
# 节奏: 默认最多补试 2 次，等待 2s/6s（最坏多花 8s，仍在 talk 60s 预算内）。
#       NPC_LLM_RETRY_DELAYS="3,9" 可自定义（无条数上限，自定义长列表会突破上面的预算）。
# 覆盖: chat 全程；stream 只救**首包之前**的瞬时病（已吐出内容就不再重试，
#       避免台词重复）—— 由 _is_retryable 按异常名/status 宽判别（超时/断连/429/5xx 等）。


def _retry_delays() -> List[float]:
    raw = env_text("NPC_LLM_RETRY_DELAYS").strip()
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


def llm_retry_enabled() -> bool:
    """P1-1 观测口。"""
    from core.config_flags import env_flag
    return env_flag("NPC_LLM_RETRY")


def _chat_with_retry(provider: "ProviderProtocol", kwargs: Dict[str, Any]) -> "LLMResponse":
    from core.config_flags import env_flag
    delays = _retry_delays() if env_flag("NPC_LLM_RETRY") else []
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
            from core.factory import create_provider, current_llm_config, hint_list

            status = current_llm_config()
            if not status["ready"]:
                # 响亮失败：说清缺哪几项（未配齐时服务照常起，走角色卡 rules 兜底）
                raise RuntimeError(
                    "LLM 三件套未配齐，缺: " + "、".join(hint_list(status["missing"]))
                )
            self._provider = create_provider(
                api_key=config.api_key(),
                model_name=status["model"],
                base_url=status["base_url"],
                temperature=config.temperature(),
                max_tokens=config.max_tokens(),
            )

        # 从 provider 同步属性 (向后兼容)
        self.model = self._provider.model_name
        self.temperature = getattr(self._provider, '_temperature', config.temperature())
        self.max_tokens = getattr(self._provider, '_max_tokens', config.max_tokens())

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
          reasoning_effort: 思考档位（None = 不指定；"off"/"disabled"/"none" = 显式关闭；
            "minimal"/"low"/"medium"/"high"/"xhigh"/"max" = 开启，按端点方言归一）
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
          reasoning_effort: 思考档位（None = 不指定；"off"/"disabled"/"none" = 显式关闭；
            "minimal"/"low"/"medium"/"high"/"xhigh"/"max" = 开启，按端点方言归一）
        """
        kwargs = dict(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        delays = _retry_delays() if llm_retry_enabled() else []
        attempt = 0
        while True:
            emitted = False
            try:
                async for chunk in self._provider.stream(**kwargs):
                    emitted = True
                    yield chunk
                return
            except Exception as exc:
                # 首包之前才补试；已吐过内容就原样抛（重试会把台词再说一遍）
                if emitted or attempt >= len(delays) or not _is_retryable(exc):
                    raise
                wait = delays[attempt]
                attempt += 1
                log.warning("llm_retry_scheduled", attempt=attempt,
                            delay_s=wait, error=str(exc)[:120])
                await asyncio.sleep(wait)

    def close(self) -> None:
        """释放底层 HTTP 连接（换脑时调用，别等 GC）。"""
        close = getattr(self._provider, "close", None)
        if callable(close):
            close()

    @property
    def provider(self) -> "ProviderProtocol":
        """获取底层 Provider (调试用)。"""
        return self._provider

    @property
    def replays_reasoning_content(self) -> bool:
        """当前 Provider 是否要求回传 reasoning_content（DeepSeek 带 tools 时强制）。"""
        return bool(getattr(self._provider, "replays_reasoning_content", False))
