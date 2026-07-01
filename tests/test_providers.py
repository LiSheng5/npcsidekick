"""
测试 Provider 层 — ProviderProtocol, OpenAIProvider, Factory。
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from agent.providers.base import ProviderProtocol
from agent.providers.openai_provider import OpenAIProvider
from agent.providers.factory import (
    create_provider,
    detect_provider_name,
    detect_base_url,
)
from agent.llm.client import LLMResponse
from agent.llm.types import StreamChunk


# ── Tests: ProviderProtocol ────────────────────────────────


class TestProviderProtocol:
    """ProviderProtocol 抽象基类测试。"""

    def test_cannot_instantiate_directly(self):
        """不能直接实例化抽象类。"""
        with pytest.raises(TypeError):
            ProviderProtocol()

    def test_concrete_subclass_works(self):
        """具体子类可以实例化。"""
        provider = OpenAIProvider(
            api_key="test-key",
            base_url="https://test.api.com",
            model="test-model",
        )
        assert isinstance(provider, ProviderProtocol)
        assert provider.model_name == "test-model"


# ── Tests: OpenAIProvider ──────────────────────────────────


class TestOpenAIProvider:
    """OpenAIProvider 测试。"""

    @pytest.fixture
    def mock_openai_class(self, mocker):
        """Mock OpenAI 类，返回 mock 实例。"""
        mock_instance = MagicMock()
        mock_class = mocker.patch(
            'agent.providers.openai_provider.OpenAI',
            return_value=mock_instance,
        )
        return mock_class, mock_instance

    @pytest.fixture
    def mock_async_openai_class(self, mocker):
        """Mock AsyncOpenAI 类。"""
        mock_instance = MagicMock()
        mock_instance.chat.completions.create = AsyncMock()
        mock_class = mocker.patch(
            'agent.providers.openai_provider.AsyncOpenAI',
            return_value=mock_instance,
        )
        return mock_class, mock_instance

    def _setup_chat_response(self, mock_instance, content="", tool_calls=None,
                              finish_reason="stop", model="test", usage=None):
        """Helper: setup mock completion chain for chat()."""
        mock_choice = MagicMock()
        mock_choice.message.content = content
        mock_choice.message.tool_calls = tool_calls
        mock_choice.finish_reason = finish_reason

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.model = model
        mock_completion.usage = usage or {}

        mock_instance.chat.completions.create.return_value = mock_completion
        return mock_instance

    def test_init_requires_api_key(self):
        """空 API Key 应该抛出异常。"""
        with pytest.raises(RuntimeError, match="API Key"):
            OpenAIProvider(api_key="", base_url="https://test.com", model="test")

    def test_init_creates_sync_client(self, mock_openai_class):
        """初始化时应该创建同步 OpenAI 客户端。"""
        mock_class, mock_instance = mock_openai_class
        provider = OpenAIProvider(
            api_key="sk-test",
            base_url="https://api.test.com",
            model="test-model",
        )
        assert mock_class.called
        call_kwargs = mock_class.call_args.kwargs
        assert call_kwargs["api_key"] == "sk-test"
        assert call_kwargs["base_url"] == "https://api.test.com"

    def test_model_name_property(self, mock_openai_class):
        """model_name 属性应该返回模型名。"""
        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://test.com", model="gpt-4",
        )
        assert provider.model_name == "gpt-4"

    def test_chat_returns_llm_response(self, mock_openai_class):
        """chat() 应该返回 LLMResponse。"""
        _, mock_instance = mock_openai_class
        self._setup_chat_response(mock_instance, content="Hello, world!")

        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://test.com", model="gpt-4",
        )
        response = provider.chat(
            messages=[{"role": "user", "content": "Hi"}],
        )

        assert isinstance(response, LLMResponse)
        assert response.content == "Hello, world!"
        assert response.finish_reason == "stop"
        assert response.model == "test"

    def test_chat_with_tools(self, mock_openai_class):
        """chat() 应该正确传递 tools 参数。"""
        _, mock_instance = mock_openai_class
        self._setup_chat_response(mock_instance)

        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://test.com", model="test",
        )
        tools = [{"type": "function", "function": {"name": "get_time"}}]
        provider.chat(
            messages=[{"role": "user", "content": "time?"}],
            tools=tools,
            tool_choice="required",
        )

        create_call = mock_instance.chat.completions.create
        assert create_call.called
        call_kwargs = create_call.call_args.kwargs
        assert call_kwargs["tools"] == tools
        assert call_kwargs["tool_choice"] == "required"

    def test_chat_with_response_format(self, mock_openai_class):
        """chat() 应该正确传递 response_format。"""
        _, mock_instance = mock_openai_class
        self._setup_chat_response(mock_instance, content='{"key": "value"}')

        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://test.com", model="test",
        )
        provider.chat(
            messages=[{"role": "user", "content": "json"}],
            response_format={"type": "json_object"},
        )

        call_kwargs = mock_instance.chat.completions.create.call_args.kwargs
        assert call_kwargs["response_format"] == {"type": "json_object"}

    def test_chat_custom_temperature(self, mock_openai_class):
        """chat() 应该支持覆盖 temperature 和 max_tokens。"""
        _, mock_instance = mock_openai_class
        self._setup_chat_response(mock_instance, content="ok")

        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://test.com", model="test",
            temperature=0.5, max_tokens=2000,
        )
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.9, max_tokens=500,
        )

        call_kwargs = mock_instance.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.9
        assert call_kwargs["max_tokens"] == 500

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self, mock_openai_class, mock_async_openai_class):
        """stream() 应该 yield StreamChunk 对象。"""
        _, mock_async_instance = mock_async_openai_class

        # Setup fake stream events
        def make_event(content="", finish_reason=None):
            event = MagicMock()
            event.model = "test-model"
            choice = MagicMock()
            if content:
                choice.delta = MagicMock()
                choice.delta.content = content
                choice.delta.tool_calls = None
            else:
                choice.delta = None
            choice.finish_reason = finish_reason
            event.choices = [choice]
            return event

        events = [
            make_event(content="Hello"),
            make_event(content=" world"),
            make_event(content="!", finish_reason="stop"),
        ]

        async def fake_stream():
            for e in events:
                yield e

        mock_async_instance.chat.completions.create.return_value = fake_stream()

        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://test.com", model="test",
        )
        chunks = []
        async for chunk in provider.stream(
            messages=[{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)

        assert len(chunks) == 3
        assert chunks[0].content == "Hello"
        assert chunks[1].content == " world"
        assert chunks[2].is_final is True

    def test_chat_with_structured_output(self, mock_openai_class):
        """chat_with_structured_output 是 chat() 的便捷包装。"""
        _, mock_instance = mock_openai_class
        self._setup_chat_response(
            mock_instance,
            content=None,
            tool_calls=[MagicMock()],
            finish_reason="tool_calls",
        )

        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://test.com", model="test",
        )
        tools = [{"type": "function", "function": {"name": "test"}}]
        response = provider.chat_with_structured_output(
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
        )

        assert response.has_tool_calls is True

    def test_repr(self, mock_openai_class):
        """__repr__ 应该显示 provider 信息。"""
        provider = OpenAIProvider(
            api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4",
        )
        r = repr(provider)
        assert "OpenAIProvider" in r
        assert "gpt-4" in r


# ── Tests: Factory ─────────────────────────────────────────


class TestFactory:
    """Provider Factory 测试。"""

    def test_detect_deepseek(self):
        """含 deepseek 的模型名 → deepseek。"""
        assert detect_provider_name("deepseek-chat") == "deepseek"
        assert detect_provider_name("deepseek-coder") == "deepseek"
        assert detect_provider_name("DEEPSEEK-V3") == "deepseek"

    def test_detect_openai(self):
        """含 gpt/openai/o1 的模型名 → openai。"""
        assert detect_provider_name("gpt-4") == "openai"
        assert detect_provider_name("gpt-4-turbo") == "openai"
        assert detect_provider_name("o1-mini") == "openai"
        assert detect_provider_name("o3") == "openai"

    def test_detect_defaults_to_unknown(self):
        """未知模型名 → unknown。"""
        assert detect_provider_name("unknown-model") == "unknown"
        assert detect_provider_name("custom-llm") == "unknown"

    def test_detect_base_url_known(self):
        """已知 provider → 使用默认 base_url (忽略配置)。"""
        assert detect_base_url("deepseek-chat", "https://custom.com") == "https://api.deepseek.com"
        assert detect_base_url("gpt-4", "https://custom.com") == "https://api.openai.com/v1"

    def test_detect_base_url_unknown(self):
        """未知 provider → 使用配置的 URL。"""
        assert detect_base_url("custom-model", "https://custom.com/v1") == "https://custom.com/v1"
        assert detect_base_url("custom-model", "") == ""

    def test_create_provider_returns_openai_provider(self):
        """create_provider 返回 OpenAIProvider 实例。"""
        provider = create_provider(
            api_key="sk-test",
            model_name="deepseek-chat",
        )
        assert isinstance(provider, OpenAIProvider)
        assert provider.model_name == "deepseek-chat"
        # DeepSeek 使用自己的 URL
        assert provider.base_url == "https://api.deepseek.com"

    def test_create_provider_openai(self):
        """GPT 模型 → OpenAI URL。"""
        provider = create_provider(
            api_key="sk-test",
            model_name="gpt-4",
        )
        assert provider.model_name == "gpt-4"
        assert provider.base_url == "https://api.openai.com/v1"

    def test_create_provider_custom_base_url(self):
        """显式 base_url 覆盖推断。"""
        provider = create_provider(
            api_key="sk-test",
            model_name="custom-model",
            base_url="https://my-llm.local/v1",
        )
        assert provider.base_url == "https://my-llm.local/v1"

    def test_create_provider_passes_temperature(self):
        """temperature 和 max_tokens 应该传递给 Provider。"""
        provider = create_provider(
            api_key="sk-test",
            model_name="gpt-4",
            temperature=0.7,
            max_tokens=8000,
        )
        assert provider._temperature == 0.7
        assert provider._max_tokens == 8000
