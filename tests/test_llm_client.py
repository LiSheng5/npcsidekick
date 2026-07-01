"""
测试 LLMClient — 同步 chat() 和异步 stream()。
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from agent.llm.client import LLMClient, LLMResponse
from agent.llm.types import StreamChunk, StreamEvent, StreamEventType


# ── Tests: StreamChunk ─────────────────────────────────────


class TestStreamChunk:
    """StreamChunk 数据类测试。"""

    def test_empty_chunk(self):
        chunk = StreamChunk()
        assert chunk.has_content is False
        assert chunk.has_tool_call is False
        assert chunk.is_final is False

    def test_content_chunk(self):
        chunk = StreamChunk(content="hello", index=1)
        assert chunk.has_content is True
        assert chunk.has_tool_call is False

    def test_tool_call_chunk(self):
        chunk = StreamChunk(
            tool_call_delta={"function_name": "get_time", "function_arguments": "{}"},
            index=5,
        )
        assert chunk.has_content is False
        assert chunk.has_tool_call is True

    def test_final_chunk(self):
        chunk = StreamChunk(finish_reason="stop", index=10)
        assert chunk.is_final is True

    def test_to_dict(self):
        chunk = StreamChunk(content="hi", finish_reason="stop", model="test", index=1)
        d = chunk.to_dict()
        assert d["content"] == "hi"
        assert d["finish_reason"] == "stop"

    def test_repr(self):
        chunk = StreamChunk(content="hello", index=3)
        r = repr(chunk)
        assert "StreamChunk" in r
        assert "hello" in r


# ── Tests: StreamEvent ─────────────────────────────────────


class TestStreamEvent:
    """StreamEvent 数据类 + 工厂方法测试。"""

    def test_event_type(self):
        event = StreamEvent(type=StreamEventType.THINKING, message="test")
        assert event.type == StreamEventType.THINKING

    def test_thinking_factory(self):
        event = StreamEvent.thinking("分析中...")
        assert event.type == StreamEventType.THINKING
        assert event.message == "分析中..."

    def test_plan_ready_factory(self):
        event = StreamEvent.plan_ready(goal="测试目标", steps_count=3)
        assert event.type == StreamEventType.PLAN_READY
        assert event.data["goal"] == "测试目标"
        assert event.data["steps_count"] == 3

    def test_step_start_factory(self):
        event = StreamEvent.step_start(step_id=1, description="读取文件", tool="read_file")
        assert event.type == StreamEventType.STEP_START
        assert event.data["step_id"] == 1
        assert event.data["tool"] == "read_file"

    def test_tool_call_factory(self):
        event = StreamEvent.tool_call(tool_name="web_search", step_id=2)
        assert event.type == StreamEventType.TOOL_CALL
        assert event.data["tool_name"] == "web_search"

    def test_tool_result_factory(self):
        event = StreamEvent.tool_result(tool_name="read_file", ok=True, step_id=1)
        assert event.type == StreamEventType.TOOL_RESULT
        assert event.data["ok"] is True

    def test_step_done_factory(self):
        event = StreamEvent.step_done(step_id=1, success=True)
        assert event.type == StreamEventType.STEP_DONE
        assert event.data["success"] is True

    def test_reflection_factory(self):
        event = StreamEvent.reflection(decision="retry", reason="timeout")
        assert event.type == StreamEventType.REFLECTION
        assert event.data["decision"] == "retry"

    def test_error_factory(self):
        event = StreamEvent.error(message="连接失败", exception=ValueError("test"))
        assert event.type == StreamEventType.ERROR

    def test_done_factory(self):
        event = StreamEvent.done(answer="完成了", duration_sec=1.5)
        assert event.type == StreamEventType.DONE
        assert event.data["answer"] == "完成了"

    def test_timestamp_auto_generated(self):
        event = StreamEvent(type=StreamEventType.THINKING, message="x")
        assert event.timestamp  # ISO format timestamp

    def test_metadata_passthrough(self):
        event = StreamEvent.thinking("x", custom_key="value")
        assert event.metadata["custom_key"] == "value"


# ── Helpers ───────────────────────────────────────────────


def _make_mock_provider(stream_chunks=None, chat_response=None):
    """Create a mock ProviderProtocol for testing LLMClient."""
    from agent.providers.base import ProviderProtocol
    provider = MagicMock(spec=ProviderProtocol)
    provider.model_name = "mock-model"

    if chat_response is None:
        chat_response = LLMResponse(
            content="mock response",
            tool_calls=[],
            finish_reason="stop",
            model="mock-model",
            usage=None,
        )
    provider.chat.return_value = chat_response

    if stream_chunks is not None:
        async def mock_stream(**kwargs):
            for chunk in stream_chunks:
                yield chunk
        provider.stream = mock_stream
    else:
        async def empty_stream(**kwargs):
            yield StreamChunk(content="default", finish_reason="stop", model="mock", index=1)
        provider.stream = empty_stream

    return provider


async def _collect_chunks(generator):
    """Helper: collect all chunks from async generator."""
    chunks = []
    async for chunk in generator:
        chunks.append(chunk)
    return chunks


# ── Tests: LLMClient.stream() ──────────────────────────────


class TestLLMClientStream:
    """LLMClient.stream() — 委托给 ProviderProtocol。"""

    @pytest.mark.asyncio
    async def test_stream_text_only(self):
        """流式纯文本响应 — mock provider 直接 yield StreamChunk。"""
        chunks_in = [
            StreamChunk(content="Hello", index=1),
            StreamChunk(content=" world", index=2),
            StreamChunk(content="!", finish_reason="stop", model="test", index=3),
        ]
        provider = _make_mock_provider(stream_chunks=chunks_in)

        client = LLMClient(provider=provider)
        chunks = await _collect_chunks(client.stream(
            messages=[{"role": "user", "content": "hi"}],
        ))

        assert len(chunks) == 3
        assert chunks[0].content == "Hello"
        assert chunks[1].content == " world"
        assert chunks[2].is_final is True
        assert chunks[2].finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_stream_with_tool_call(self):
        """流式工具调用 — tool_call_delta 传递正确。"""
        chunks_in = [
            StreamChunk(
                tool_call_delta={"function_name": "get_time", "function_arguments": '{"tz"', "index": 0, "id": "call_1"},
                index=1,
            ),
            StreamChunk(
                tool_call_delta={"function_name": None, "function_arguments": ': "UTC"}', "index": 0, "id": None},
                index=2,
            ),
            StreamChunk(finish_reason="tool_calls", model="test", index=3),
        ]
        provider = _make_mock_provider(stream_chunks=chunks_in)

        client = LLMClient(provider=provider)
        chunks = await _collect_chunks(client.stream(
            messages=[{"role": "user", "content": "what time?"}],
            tools=[{"type": "function", "function": {"name": "get_time"}}],
        ))

        assert len(chunks) == 3
        assert chunks[0].has_tool_call is True
        assert chunks[0].tool_call_delta["function_name"] == "get_time"
        assert chunks[1].has_tool_call is True
        assert chunks[2].finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_stream_parameters_passed_to_provider(self):
        """stream() 应该将参数委托给 provider.stream()。"""
        from agent.providers.base import ProviderProtocol

        # 使用 MagicMock 的 stream 方法 (而不是真的 async function)
        provider = MagicMock(spec=ProviderProtocol)
        provider.model_name = "mock-model"
        provider.chat.return_value = LLMResponse(
            content="ok", tool_calls=[], finish_reason="stop", model="m", usage=None,
        )

        async def mock_stream(**kwargs):
            yield StreamChunk(content="done", finish_reason="stop", model="test", index=1)

        provider.stream = mock_stream

        client = LLMClient(provider=provider)
        chunks = await _collect_chunks(client.stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "test"}}],
            tool_choice="required",
            temperature=0.5,
            max_tokens=100,
        ))

        assert len(chunks) == 1
        assert chunks[0].content == "done"

    @pytest.mark.asyncio
    async def test_chat_delegates_to_provider(self):
        """chat() 应该委托给 provider.chat()。"""
        provider = _make_mock_provider()
        client = LLMClient(provider=provider)

        response = client.chat(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.5,
        )

        assert provider.chat.called
        assert response.content == "mock response"
        call_kwargs = provider.chat.call_args.kwargs
        assert call_kwargs["temperature"] == 0.5

    def test_provider_injection(self):
        """注入 provider 后应使用它而非工厂创建。"""
        provider = _make_mock_provider()
        client = LLMClient(provider=provider)

        assert client._provider is provider
        assert client.model == "mock-model"

    def test_model_attribute_sync(self):
        """model 属性应该从 provider.model_name 同步。"""
        provider = _make_mock_provider()
        provider.model_name = "gpt-4-turbo"

        client = LLMClient(provider=provider)
        assert client.model == "gpt-4-turbo"
