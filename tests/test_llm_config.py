"""LLM 三件套配置测试 —— 用户自配 / 配置优先 / 缺配置响亮提示（全零网络）。

口径（P-19）：模型与端点由用户自己配，v4 不绑厂商；显式端点永不被推断覆盖；
配不齐时服务照起、/api/talk 走角色卡 rules 兜底，并把缺哪几项写出来。
"""
from __future__ import annotations

import pytest

from core import factory
from core.openai_provider import OpenAIProvider


def _provider(**kwargs) -> OpenAIProvider:
    """不联网的 Provider 实例（只测参数拼装，不发请求）。"""
    return OpenAIProvider(api_key="k", base_url="http://localhost:11434/v1",
                          model="m", **kwargs)


# ── 端点解析：配置优先 ────────────────────────────────────

def test_configured_base_url_wins():
    """回归：显式端点不再被厂商推断覆盖（以前会被强行改回官方地址）。"""
    provider = factory.create_provider(api_key="k", model_name="deepseek-v4-pro",
                                       base_url="https://my-gateway.local/v1")
    assert provider.base_url == "https://my-gateway.local/v1"


def test_inferred_base_url_when_not_configured():
    provider = factory.create_provider(api_key="k", model_name="deepseek-v4-pro")
    assert provider.base_url == "https://api.deepseek.com"


def test_unknown_model_without_base_url_raises():
    with pytest.raises(ValueError) as exc:
        factory.create_provider(api_key="k", model_name="my-custom-model")
    assert "NPC_BASE_URL" in str(exc.value)


def test_provider_keywords_cover_popular_vendors():
    assert factory.detect_provider_name("deepseek-v4-flash") == "deepseek"
    assert factory.detect_provider_name("qwen-plus") == "qwen"
    assert factory.detect_provider_name("kimi-k3") == "moonshot"
    assert factory.detect_provider_name("doubao-pro-32k") == "ark"
    assert factory.detect_provider_name("grok-4") == "xai"
    assert factory.detect_provider_name("gemini-3-pro") == "gemini"
    assert factory.detect_provider_name("claude-fable-5") == "claude"
    assert factory.detect_provider_name("glm-5.2") == "zhipu"
    assert factory.detect_provider_name("totally-unknown-9b") == "unknown"


# ── 三件套状态 ───────────────────────────────────────────

def test_resolve_llm_config_reports_every_missing_item():
    status = factory.resolve_llm_config(api_key="", model_name="", base_url="")
    assert status["ready"] is False
    assert status["missing"] == ["NPC_API_KEY", "AGENT_MODEL", "NPC_BASE_URL"]


def test_resolve_llm_config_infers_endpoint_from_model():
    status = factory.resolve_llm_config(api_key="k", model_name="gpt-5", base_url="")
    assert status["ready"] is True
    assert status["source"] == "inferred"
    assert status["base_url"] == "https://api.openai.com/v1"


def test_resolve_llm_config_prefers_configured_endpoint():
    status = factory.resolve_llm_config(api_key="k", model_name="whatever-model",
                                        base_url="http://localhost:11434/v1")
    assert status["ready"] is True
    assert status["source"] == "configured"
    assert status["base_url"] == "http://localhost:11434/v1"


def test_resolve_llm_config_unknown_model_needs_endpoint():
    status = factory.resolve_llm_config(api_key="k", model_name="whatever-model", base_url="")
    assert status["ready"] is False
    assert status["missing"] == ["NPC_BASE_URL"]


def test_hint_list_is_actionable():
    hints = factory.hint_list(["NPC_API_KEY", "AGENT_MODEL"])
    assert "NPC_API_KEY" in hints[0] and "DEEPSEEK_API_KEY" in hints[0]
    assert "AGENT_MODEL" in hints[1]


# ── 逃生舱：NPC_LLM_EXTRA_BODY ───────────────────────────

def test_extra_body_lands_in_request_body(monkeypatch):
    monkeypatch.setenv("NPC_LLM_EXTRA_BODY", '{"reasoning": {"effort": "high"}}')
    kwargs = _provider()._apply_extra_body({"model": "m", "messages": []})
    assert kwargs["extra_body"] == {"reasoning": {"effort": "high"}}


def test_extra_body_top_level_keys_override(monkeypatch):
    monkeypatch.setenv("NPC_LLM_EXTRA_BODY",
                       '{"max_completion_tokens": 4096, "temperature": 1}')
    kwargs = _provider()._apply_extra_body(
        {"model": "m", "messages": [], "temperature": 0.2})
    assert kwargs["max_completion_tokens"] == 4096
    assert kwargs["temperature"] == 1


def test_extra_body_overrides_thinking(monkeypatch):
    monkeypatch.setenv("NPC_LLM_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')
    kwargs = _provider()._apply_extra_body(
        {"model": "m", "extra_body": {"thinking": {"type": "enabled"}}})
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}


def test_extra_body_bad_json_is_ignored(monkeypatch):
    monkeypatch.setenv("NPC_LLM_EXTRA_BODY", "{ not json")
    provider = _provider()
    assert provider._user_body() == {}
    assert "extra_body" not in provider._apply_extra_body({"model": "m"})


def test_extra_body_can_be_injected_directly():
    assert _provider(extra_body={"seed": 7})._user_body() == {"seed": 7}


# ── server 侧：缺配置响亮提示 ────────────────────────────

def test_state_reports_missing_llm_config(tmp_store, write_persona, make_app_client,
                                          monkeypatch):
    from core import config
    import server

    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("NPC_MODEL", raising=False)
    monkeypatch.delenv("NPC_BASE_URL", raising=False)

    llm = make_app_client().get("/api/state").json()["llm"]

    assert server.llm_config_status()["ready"] is False
    assert llm["ready"] is False
    assert "AGENT_MODEL" in llm["missing"]
    assert "NPC_API_KEY" in llm["missing"]


def test_build_default_client_returns_none_when_unconfigured(monkeypatch):
    from core import config
    import server

    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("NPC_BASE_URL", raising=False)

    assert server.build_default_client() is None


def test_build_default_client_uses_configured_endpoint(monkeypatch):
    from core import config
    import server

    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setenv("AGENT_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("NPC_BASE_URL", "https://gateway.local/v1")

    client = server.build_default_client()

    assert client is not None
    assert client.provider.base_url == "https://gateway.local/v1"
