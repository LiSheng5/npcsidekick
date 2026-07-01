"""
LLM Client — 统一的 LLM 调用接口。

支持:
  - OpenAI 兼容 API (DeepSeek / OpenAI / 任何兼容服务)
  - 工具调用 (function calling)
  - 流式和非流式
  - 结构化输出 (JSON mode)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from openai import OpenAI

import config


class LLMClient:
    """
    统一的 LLM 客户端。

    使用方式:
      client = LLMClient()
      response = client.chat(messages, tools=[...])
      if response.has_tool_calls:
          ...
    """

    def __init__(self):
        api_key = config.API_KEY
        if not api_key:
            raise RuntimeError(
                "请设置 API_KEY (在 config.py) 或环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY"
            )
        self._client = OpenAI(api_key=api_key, base_url=config.BASE_URL)
        self.model = config.MODEL_NAME
        self.temperature = config.TEMPERATURE
        self.max_tokens = config.MAX_TOKENS

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]] = None,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict] = None,  # {"type": "json_object"} for JSON mode
    ) -> "LLMResponse":
        """
        发送对话请求，返回 LLMResponse。

        Args:
          messages: OpenAI 格式的消息列表
          tools: OpenAI tools 数组 (可选)
          tool_choice: "auto" | "none" | "required" | {"type": "function", "function": {"name": "..."}}
          temperature: 覆盖默认温度
          max_tokens: 覆盖默认 max_tokens
          response_format: {"type": "json_object"} 强制 JSON 输出
        """
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature or self.temperature,
            max_tokens=max_tokens or self.max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if response_format:
            kwargs["response_format"] = response_format

        completion = self._client.chat.completions.create(**kwargs)
        choice = completion.choices[0]

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=list(choice.message.tool_calls) if choice.message.tool_calls else [],
            finish_reason=choice.finish_reason,
            model=completion.model,
            usage=completion.usage,
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
