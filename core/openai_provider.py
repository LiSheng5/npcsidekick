"""
OpenAIProvider — OpenAI / DeepSeek / 任何 OpenAI 兼容 API 的 Provider 实现。

支持:
  - OpenAI 官方 API (api.openai.com)
  - DeepSeek API (api.deepseek.com)
  - 任何兼容 OpenAI chat/completions 格式的服务
"""

from __future__ import annotations

import json
import os
import re

from typing import Any, AsyncGenerator, Dict, List, Optional

from openai import OpenAI, AsyncOpenAI

from core.client import LLMResponse
from core.logging_config import log
from core.types import StreamChunk
from core.base import ProviderProtocol


# NPC_LLM_EXTRA_BODY 里允许直接覆盖的 SDK 具名参数（其余字段一律进 extra_body）
_TOP_LEVEL_KEYS = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "stop", "seed",
    "parallel_tool_calls", "tool_choice", "response_format", "user",
})


class OpenAIProvider(ProviderProtocol):
    """
    OpenAI 兼容 API 的 Provider 实现。

    同一个类支持 OpenAI 和 DeepSeek — 区别仅在于 base_url 和 model_name。

    使用方式:
      # OpenAI
      provider = OpenAIProvider(
          api_key="sk-...", base_url="https://api.openai.com/v1", model="gpt-4",
      )
      # DeepSeek
      provider = OpenAIProvider(
          api_key="sk-...", base_url="https://api.deepseek.com", model="deepseek-v4-pro",
      )
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        reasoning_effort: str | None = None,
        extra_body: Optional[dict] = None,
    ):
        if not api_key:
            raise RuntimeError("API Key 不能为空")

        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._extra_body = extra_body or None
        self._base_url = base_url
        self._api_key = api_key

        # 同步客户端
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        # 异步客户端 (惰性创建)
        self._async_client: Optional[AsyncOpenAI] = None

    # ── ProviderProtocol 实现 ────────────────────────────

    @property
    def model_name(self) -> str:
        return self._model

    def _thinking_body(self, re_value: str) -> dict:
        """把 reasoning_effort 档位翻译成各家 thinking body。

        OpenRouter: {reasoning:{effort:值, exclude:false}} — 响应经 message.reasoning 回传思考
        DeepSeek/OpenAI: 支持分级思考 -> {thinking:{type:enabled, reasoning_effort:值}}
        Zhipu GLM(<5.2): 仅支持开/关 -> {thinking:{type:enabled}}
        off/disabled/none        -> 显式关闭 {thinking:{type:disabled}}
        """
        val = str(re_value).lower()
        if val in ("off", "disabled", "none"):
            # OpenRouter 无显式关闭:不发 reasoning 参数即不回传思考
            if self._is_openrouter():
                return {}
            return {"thinking": {"type": "disabled"}}
        if self._is_openrouter():
            # OpenRouter effort 仅支持 low/medium/high; max 等高档位归一到 high
            effort = val if val in ("low", "medium", "high") else "high"
            return {"reasoning": {"effort": effort, "exclude": False}}
        if not self._glm_supports_effort():
            return {"thinking": {"type": "enabled"}}
        return {"thinking": {"type": "enabled", "reasoning_effort": re_value}}

    def _user_body(self) -> dict:
        """用户透传的请求体片段（环境变量 NPC_LLM_EXTRA_BODY，现读可热切）。

        各家方言（思考参数、长度参数名…）由用户自己填，v4 不做模型画像；
        非法 JSON → 记一条 warning 后忽略，绝不因此打断对话。
        """
        merged: dict = {}
        raw = os.environ.get("NPC_LLM_EXTRA_BODY", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("llm_extra_body_bad_json", raw=raw[:120])
                parsed = None
            if isinstance(parsed, dict):
                merged.update(parsed)
            elif parsed is not None:
                log.warning("llm_extra_body_not_object", raw=raw[:120])
        if self._extra_body:
            merged.update(self._extra_body)
        return merged

    def _apply_extra_body(self, kwargs: dict) -> dict:
        """把透传片段合进请求参数：SDK 具名参数走顶层，其余进 extra_body。

        透传最后合并 —— 可覆盖 v4 自己拼的 thinking / 默认值。
        """
        user = self._user_body()
        if not user:
            return kwargs
        body = dict(kwargs.get("extra_body") or {})
        for key, value in user.items():
            if key in _TOP_LEVEL_KEYS:
                kwargs[key] = value
            else:
                body[key] = value
        if body:
            kwargs["extra_body"] = body
        return kwargs

    def _is_openrouter(self) -> bool:
        """OpenRouter 聚合端点:思考请求/响应字段与 DeepSeek 直连不同。"""
        return "openrouter" in (self._base_url or "").lower()

    def _glm_supports_effort(self) -> bool:
        """GLM 系列仅在 5.2 及以上支持 reasoning_effort 分级思考。"""
        m = self._model.lower()
        if "glm" not in m:
            return True
        mm = re.search(r"glm[-_]?(\d+(?:\.\d+)?)", m)
        if not mm:
            return True
        try:
            return float(mm.group(1)) >= 5.2
        except ValueError:
            return True

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
        """同步非流式对话。"""
        kwargs = dict(
            model=self._model,
            messages=messages,
            temperature=temperature if temperature is not None else self._temperature,
            max_tokens=max_tokens if max_tokens is not None else self._max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if response_format:
            kwargs["response_format"] = response_format

        # reasoning_effort -> 各家 thinking (经 extra_body 传递, 见 _thinking_body)
        #   off/disabled/none -> 关闭 {thinking:{type:disabled}}
        #   deepseek/openai -> {thinking:{type:enabled, reasoning_effort:值}}
        #   zhipu GLM(<5.2) -> {thinking:{type:enabled}} (仅支持开/关)
        #   None -> 不指定(服务端默认)
        _re = reasoning_effort if reasoning_effort is not None else self._reasoning_effort
        if _re:
            kwargs["extra_body"] = self._thinking_body(_re)
        # 用户透传（NPC_LLM_EXTRA_BODY）最后合并，可覆盖上面拼好的 thinking
        kwargs = self._apply_extra_body(kwargs)

        completion = self._client.chat.completions.create(**kwargs)
        choice = completion.choices[0]

        # 思考可视化: 兼容 OpenRouter/DeepSeek 的思考内容字段
        # (OpenRouter: message.reasoning / reasoning_details; DeepSeek: message.reasoning_content)
        reasoning = getattr(choice.message, "reasoning", None)
        if not reasoning:
            reasoning = getattr(choice.message, "reasoning_content", None)
        if not reasoning:
            extra = getattr(choice.message, "model_extra", None) or {}
            reasoning = extra.get("reasoning") or ""
        if reasoning and not isinstance(reasoning, str):
            details = getattr(reasoning, "text", None)
            reasoning = details if details else str(reasoning)

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=list(choice.message.tool_calls) if choice.message.tool_calls else [],
            finish_reason=choice.finish_reason,
            model=completion.model,
            usage=completion.usage,
            reasoning=reasoning or "",
        )

    async def stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: str | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """异步流式对话。"""
        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
            )

        kwargs = dict(
            model=self._model,
            messages=messages,
            temperature=temperature if temperature is not None else self._temperature,
            max_tokens=max_tokens if max_tokens is not None else self._max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        # reasoning_effort -> 各家 thinking (经 extra_body 传递, 见 _thinking_body)
        #   off/disabled/none -> 关闭 {thinking:{type:disabled}}
        #   deepseek/openai -> {thinking:{type:enabled, reasoning_effort:值}}
        #   zhipu GLM(<5.2) -> {thinking:{type:enabled}} (仅支持开/关)
        #   None -> 不指定(服务端默认)
        _re = reasoning_effort if reasoning_effort is not None else self._reasoning_effort
        if _re:
            kwargs["extra_body"] = self._thinking_body(_re)
        # 用户透传（NPC_LLM_EXTRA_BODY）最后合并，可覆盖上面拼好的 thinking
        kwargs = self._apply_extra_body(kwargs)

        stream_response = await self._async_client.chat.completions.create(**kwargs)

        chunk_index = 0
        async for event in stream_response:
            chunk_index += 1
            choice = event.choices[0] if event.choices else None

            if choice is None:
                continue  # Usage stats chunk

            finish_reason = choice.finish_reason
            delta = choice.delta if choice.delta else None

            if delta is None and finish_reason is None:
                continue

            content = (delta.content or "") if delta else ""

            tool_call_delta = None
            if delta is not None and delta.tool_calls:
                tc = delta.tool_calls[0]
                tool_call_delta = {
                    "index": getattr(tc, "index", 0),
                    "id": getattr(tc, "id", None),
                    "function_name": getattr(tc.function, "name", None) if tc.function else None,
                    "function_arguments": getattr(tc.function, "arguments", None) if tc.function else None,
                }

            yield StreamChunk(
                content=content,
                tool_call_delta=tool_call_delta,
                finish_reason=finish_reason,
                model=event.model,
                index=chunk_index,
            )

    # ── 便捷方法 ─────────────────────────────────────────

    def chat_with_structured_output(
        self,
        messages: List[Dict[str, str]],
        tools: List[dict],
    ) -> LLMResponse:
        """强制返回工具调用 (或文本)。"""
        return self.chat(messages, tools=tools, tool_choice="auto")

    @property
    def base_url(self) -> str:
        return self._base_url

    def __repr__(self) -> str:
        return f"<OpenAIProvider model={self._model} base_url={self._base_url}>"
