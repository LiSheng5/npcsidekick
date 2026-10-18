"""思考模式（thinking / reasoning）契约测试 —— 全零网络。

口径（DeepSeek 官方 Thinking Mode 文档）：
  · `reasoning_effort` 与 `thinking` 都在**请求体顶层**，互为兄弟，不能嵌套；
  · 请求里带 `tools` 时，历史 assistant 的 `reasoning_content` 必须原样回传，缺了 400；
  · 流式思考增量在 `delta.reasoning_content`（OpenRouter 是 `delta.reasoning`）。
回归背景：以前把 reasoning_effort 塞进 thinking 里、流式又丢掉思考内容 → 带工具时可能 400。
"""
from __future__ import annotations

import asyncio
import types

import pytest

import chatlog
from conftest import FakeProvider
from core.openai_provider import OpenAIProvider


def _provider(base_url: str = "https://api.deepseek.com", model: str = "deepseek-v4-pro"):
    return OpenAIProvider(api_key="k", base_url=base_url, model=model)


# ── 请求体：reasoning_effort 在顶层 ─────────────────────────

def test_deepseek_reasoning_effort_is_top_level():
    params = _provider()._thinking_params("high")
    assert params == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert "reasoning_effort" not in params["thinking"]        # 不许嵌进 thinking


def test_deepseek_medium_is_mapped_to_high():
    """官方映射：medium/xhigh → high，minimal → low，max → max。"""
    provider = _provider()
    assert provider._thinking_params("medium")["reasoning_effort"] == "high"
    assert provider._thinking_params("low")["reasoning_effort"] == "low"
    assert provider._thinking_params("max")["reasoning_effort"] == "max"


def test_deepseek_off_disables_thinking_without_effort():
    assert _provider()._thinking_params("off") == {"thinking": {"type": "disabled"}}


def test_openai_official_uses_top_level_effort_only():
    """OpenAI 官方没有 thinking 开关：只发顶层 reasoning_effort，且不发 max。"""
    provider = _provider("https://api.openai.com/v1", "gpt-5")
    assert provider._thinking_params("max") == {"reasoning_effort": "high"}
    assert provider._thinking_params("medium") == {"reasoning_effort": "medium"}
    assert provider._thinking_params("off") == {}               # 无关闭开关 → 一个都不发


def test_openrouter_keeps_unified_reasoning_field():
    provider = _provider("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-pro")
    assert provider._thinking_params("high") == {
        "reasoning": {"effort": "high", "exclude": False}}
    assert provider._thinking_params("off") == {}


def test_glm_below_52_only_switches():
    provider = _provider("https://open.bigmodel.cn/api/paas/v4", "glm-4.6")
    assert provider._thinking_params("high") == {"thinking": {"type": "enabled"}}


def test_chat_puts_thinking_params_in_request_body(monkeypatch):
    """真·请求参数：extra_body 的键就是 HTTP 请求体顶层键，不被再包一层。"""
    provider = _provider()
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop before network")

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)
    with pytest.raises(RuntimeError):
        provider.chat([{"role": "user", "content": "在吗"}], reasoning_effort="high")

    assert captured["extra_body"] == {"thinking": {"type": "enabled"},
                                     "reasoning_effort": "high"}
    assert "thinking" not in captured and "reasoning_effort" not in captured   # 都在 body 里


# ── 流式：思考增量不许丢 ───────────────────────────────────

class _FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _event(content: str = "", reasoning: str = "", finish_reason=None):
    delta = types.SimpleNamespace(content=content, reasoning_content=reasoning or None,
                                  reasoning=None, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], model="deepseek-v4-pro")


def test_stream_parses_reasoning_content(monkeypatch):
    provider = _provider()
    events = [_event(reasoning="先想"), _event(reasoning="一想"),
              _event(content="嗯。", finish_reason="stop")]
    provider._async_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=_async_return(_FakeStream(events)))))

    chunks = asyncio.run(_collect(provider.stream([{"role": "user", "content": "在吗"}])))

    assert "".join(c.reasoning for c in chunks) == "先想一想"
    assert "".join(c.content for c in chunks) == "嗯。"


async def _collect(agen):
    return [chunk async for chunk in agen]


def _async_return(value):
    async def _inner(**kwargs):
        return value
    return _inner


# ── 带 tools：reasoning_content 回传 + 落盘 ────────────────

class ReplayProvider(FakeProvider):
    """模拟 DeepSeek：要求回传 reasoning_content。"""

    replays_reasoning_content = True


def test_deepseek_provider_declares_replay():
    assert _provider().replays_reasoning_content is True
    assert _provider("https://api.openai.com/v1", "gpt-5").replays_reasoning_content is False
    assert _provider("https://openrouter.ai/api/v1", "x").replays_reasoning_content is False
    # OpenRouter 跑 deepseek/* 也走它自己的 reasoning 协议，不回传 reasoning_content
    assert _provider("https://openrouter.ai/api/v1",
                     "deepseek/deepseek-v4-pro").replays_reasoning_content is False


def _talk_once(make_app_client, write_persona, provider):
    write_persona("cang")
    client = make_app_client(provider=provider)
    return client.post("/api/talk", json={"npc_id": "cang", "message": "记一下"})


def _assistant_tool_messages(call: dict):
    return [m for m in call["messages"]
            if m["role"] == "assistant" and m.get("tool_calls")]


def test_tool_round_replays_reasoning_content(tmp_store, write_persona, make_app_client):
    provider = ReplayProvider(turns=[
        {"text": "记下了。", "reasoning": "玩家说了件事，先记下来。",
         "tool_calls": [{"name": "remember", "arguments": {"content": "玩家爱吃面"}}]},
        {"text": "嗯。", "reasoning": "收尾。"},
    ])

    _talk_once(make_app_client, write_persona, provider)

    assert len(provider.stream_calls) == 2
    tool_msgs = _assistant_tool_messages(provider.stream_calls[1])
    assert tool_msgs and tool_msgs[0]["reasoning_content"] == "玩家说了件事，先记下来。"


def test_non_deepseek_does_not_send_reasoning_content(tmp_store, write_persona,
                                                      make_app_client, make_provider):
    provider = make_provider(turns=[
        {"text": "记下了。", "reasoning": "先记下来。",
         "tool_calls": [{"name": "remember", "arguments": {"content": "玩家爱吃面"}}]},
        {"text": "嗯。"},
    ])

    _talk_once(make_app_client, write_persona, provider)

    tool_msgs = _assistant_tool_messages(provider.stream_calls[1])
    assert tool_msgs and "reasoning_content" not in tool_msgs[0]


def test_reasoning_persisted_and_replayed_in_history(tmp_store, write_persona,
                                                    make_app_client):
    provider = ReplayProvider(turns=[
        {"text": "记下了。", "reasoning": "先记下来。",
         "tool_calls": [{"name": "remember", "arguments": {"content": "玩家爱吃面"}}]},
        {"text": "嗯。", "reasoning": "收尾。"},
    ])

    client = _talk_once(make_app_client, write_persona, provider)
    assert client.status_code == 200

    turns = chatlog.load_turns("cang", store_dir=tmp_store)
    assert turns[-1]["reasoning"] == "收尾。"          # 最终答复那一轮落盘

    replayed = chatlog.build_history("cang", provider, token_budget=1000,
                                     store_dir=tmp_store, summarize=False,
                                     reasoning_replay=True)
    assert replayed[-1]["reasoning_content"] == "收尾。"

    plain = chatlog.build_history("cang", provider, token_budget=1000,
                                  store_dir=tmp_store, summarize=False,
                                  reasoning_replay=False)
    assert "reasoning_content" not in plain[-1]


def test_reasoning_counts_toward_history_budget(tmp_store):
    """思考内容会被一起回传，就必须算进对话层预算（否则上下文悄悄超支）。"""
    chatlog.append_turn("cang", "问" * 5, "答" * 5, store_dir=tmp_store,
                        reasoning="想" * 100)
    turns = chatlog.load_turns("cang", store_dir=tmp_store)
    assert turns[-1]["reasoning"] == "想" * 100

    # 预算 20：只算正文能装下两条；把 100 字思考算进去就只留最新一条
    assert [m["role"] for m in chatlog.select_recent(turns, 20)] == ["assistant"]
