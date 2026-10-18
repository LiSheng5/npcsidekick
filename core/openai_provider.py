"""
OpenAIProvider — OpenAI / DeepSeek / 任何 OpenAI 兼容 API 的 Provider 实现。

支持:
  - OpenAI 官方 API (api.openai.com)
  - DeepSeek API (api.deepseek.com)
  - 任何兼容 OpenAI chat/completions 格式的服务
"""

from __future__ import annotations

import json
import re

from typing import Any, AsyncGenerator, Dict, List, Optional

from openai import OpenAI, AsyncOpenAI

from core.client import LLMResponse
from core.config_flags import env_text
from core.logging_config import log
from core.types import StreamChunk
from core.base import ProviderProtocol


# NPC_LLM_EXTRA_BODY 里允许直接覆盖的 SDK 具名参数（其余字段一律进 extra_body）
_TOP_LEVEL_KEYS = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "stop", "seed",
    "parallel_tool_calls", "tool_choice", "response_format", "user",
})

# 思考档位归一：DeepSeek 官方只认 none/low/high/max（minimal→low、medium/xhigh→high）；
# OpenAI 官方认 minimal/low/medium/high（max 归一到 high）。
_DEEPSEEK_EFFORT = {
    "minimal": "low", "low": "low", "medium": "high",
    "high": "high", "xhigh": "high", "max": "max",
}
_OPENAI_EFFORT = {
    "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "high",
}
_OFF_VALUES = frozenset({"off", "disabled", "none"})


def _reasoning_text(payload: Any) -> str:
    """把各家的思考内容统一成纯文本（可能是 str / 对象 / 对象列表）。"""
    if not payload:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (list, tuple)):
        return "".join(_reasoning_text(item) for item in payload)
    text = getattr(payload, "text", None)
    if isinstance(text, str):
        return text
    return str(payload)


def _message_reasoning(message: Any) -> str:
    """非流式 message 里的思考内容：OpenRouter: reasoning；DeepSeek: reasoning_content。"""
    for attr in ("reasoning", "reasoning_content"):
        value = getattr(message, attr, None)
        if value:
            return _reasoning_text(value)
    extra = getattr(message, "model_extra", None) or {}
    return _reasoning_text(extra.get("reasoning") or extra.get("reasoning_content"))


def _delta_reasoning(delta: Any) -> str:
    """流式 delta 里的思考增量（DeepSeek: reasoning_content，OpenRouter: reasoning）。"""
    if delta is None:
        return ""
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(delta, attr, None)
        if value:
            return _reasoning_text(value)
    extra = getattr(delta, "model_extra", None) or {}
    return _reasoning_text(extra.get("reasoning_content") or extra.get("reasoning"))


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

    def _thinking_params(self, re_value: str) -> dict:
        """把 reasoning_effort 档位翻译成请求体字段 —— **一律顶层，绝不互相嵌套**。

        DeepSeek 官方（Thinking Mode）要求 `reasoning_effort` 与 `thinking` 平级：
            {"model": ..., "reasoning_effort": "high",
             "thinking": {"type": "enabled"}}
        把 reasoning_effort 塞进 thinking 里是非法的（服务端可能 400 或静默忽略）。

        各家方言：
          DeepSeek / 兼容网关 : {"thinking": {"type": "enabled"}, "reasoning_effort": 值}
          OpenAI 官方         : {"reasoning_effort": 值}（没有 thinking 开关）
          OpenRouter          : {"reasoning": {"effort": 值, "exclude": False}}
          Zhipu GLM(<5.2)     : {"thinking": {"type": "enabled"}}（只支持开/关）
          off/disabled/none   : DeepSeek 系 → {"thinking": {"type": "disabled"}}；
                                OpenAI / OpenRouter → 不发（它们没有关闭开关）
        """
        val = str(re_value).lower()
        off = val in _OFF_VALUES

        if self._is_openrouter():
            if off:
                # OpenRouter 无显式关闭:不发 reasoning 参数即不回传思考
                return {}
            # OpenRouter effort 仅支持 low/medium/high; max 等高档位归一到 high
            effort = val if val in ("low", "medium", "high") else "high"
            return {"reasoning": {"effort": effort, "exclude": False}}

        if self._is_openai_official():
            # OpenAI 官方只有顶层 reasoning_effort（minimal/low/medium/high），
            # 既没有 thinking 开关，也没有"关闭思考"的参数
            return {} if off else {"reasoning_effort": _OPENAI_EFFORT.get(val, "medium")}

        if self._is_glm():
            if off:
                return {"thinking": {"type": "disabled"}}
            params = {"thinking": {"type": "enabled"}}
            if self._glm_supports_effort():
                params["reasoning_effort"] = _DEEPSEEK_EFFORT.get(val, "high")
            return params

        # DeepSeek 及 OpenAI 兼容网关：两个字段都在顶层
        if off:
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"},
                "reasoning_effort": _DEEPSEEK_EFFORT.get(val, "high")}

    def _user_body(self) -> dict:
        """用户透传的请求体片段（环境变量 NPC_LLM_EXTRA_BODY，现读可热切）。

        各家方言（思考参数、长度参数名…）由用户自己填，v4 不做模型画像；
        非法 JSON → 记一条 warning 后忽略，绝不因此打断对话。
        """
        merged: dict = {}
        raw = env_text("NPC_LLM_EXTRA_BODY").strip()
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

    def _is_openai_official(self) -> bool:
        """OpenAI 官方端点:只有顶层 reasoning_effort,不认 thinking / reasoning 包装。"""
        return "api.openai.com" in (self._base_url or "").lower()

    def _is_glm(self) -> bool:
        return "glm" in (self._model or "").lower()

    def _is_deepseek(self) -> bool:
        """DeepSeek（直连或其代理/网关）—— 带 tools 时 reasoning_content 必须回传。

        OpenRouter 即使跑的是 deepseek/* 模型也走它自己的 reasoning 协议，
        别把 DeepSeek 直连的 reasoning_content 塞过去。
        """
        if self._is_openrouter():
            return False
        return ("deepseek" in (self._base_url or "").lower()
                or "deepseek" in (self._model or "").lower())

    @property
    def replays_reasoning_content(self) -> bool:
        """是否必须把 assistant 的 reasoning_content 随消息回传。

        DeepSeek 官方硬要求：请求里带 `tools` 时，历史每一轮 assistant 的
        reasoning_content 都要原样回传，缺了直接 400。其它家（OpenAI/OpenRouter）
        没有这个字段，别把未知字段塞给它们。
        """
        return self._is_deepseek()

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

        # reasoning_effort -> 各家顶层字段（见 _thinking_params；extra_body 的上层键
        # 就是 HTTP 请求体的顶层键，不会被再包一层）
        #   off/disabled/none -> DeepSeek 系 {thinking:{type:disabled}}；OpenAI/OpenRouter 不发
        #   deepseek/兼容网关 -> {thinking:{type:enabled}, reasoning_effort:值}
        #   openai 官方       -> {reasoning_effort:值}
        #   zhipu GLM(<5.2)   -> {thinking:{type:enabled}} (仅支持开/关)
        #   None -> 不指定(服务端默认)
        _re = reasoning_effort if reasoning_effort is not None else self._reasoning_effort
        if _re:
            kwargs["extra_body"] = self._thinking_params(_re)
        # 用户透传（NPC_LLM_EXTRA_BODY）最后合并，可覆盖上面拼好的思考参数
        kwargs = self._apply_extra_body(kwargs)

        completion = self._client.chat.completions.create(**kwargs)
        choice = completion.choices[0]

        # 思考可视化: 兼容 OpenRouter/DeepSeek 的思考内容字段
        # (OpenRouter: message.reasoning / reasoning_details; DeepSeek: message.reasoning_content)
        reasoning = _message_reasoning(choice.message)

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=list(choice.message.tool_calls) if choice.message.tool_calls else [],
            finish_reason=choice.finish_reason,
            model=completion.model,
            usage=completion.usage,
            reasoning=reasoning,
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

        # reasoning_effort -> 各家顶层字段（见 _thinking_params；extra_body 的上层键
        # 就是 HTTP 请求体的顶层键，不会被再包一层）
        #   off/disabled/none -> DeepSeek 系 {thinking:{type:disabled}}；OpenAI/OpenRouter 不发
        #   deepseek/兼容网关 -> {thinking:{type:enabled}, reasoning_effort:值}
        #   openai 官方       -> {reasoning_effort:值}
        #   zhipu GLM(<5.2)   -> {thinking:{type:enabled}} (仅支持开/关)
        #   None -> 不指定(服务端默认)
        _re = reasoning_effort if reasoning_effort is not None else self._reasoning_effort
        if _re:
            kwargs["extra_body"] = self._thinking_params(_re)
        # 用户透传（NPC_LLM_EXTRA_BODY）最后合并，可覆盖上面拼好的思考参数
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
            # 思考增量：DeepSeek 走 reasoning_content，OpenRouter 走 reasoning。
            # 带 tools 时 DeepSeek 要求把 CoT 原样回传，所以不能在这里丢掉。
            reasoning = _delta_reasoning(delta)

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
                reasoning=reasoning,
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
