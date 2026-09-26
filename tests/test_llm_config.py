"""LLM 三件套配置测试 —— 用户自配 / 配置优先 / 缺配置响亮提示（全零网络）。

口径（P-19）：模型与端点由用户自己配，v4 不绑厂商；显式端点永不被推断覆盖；
配不齐时服务照起、/api/talk 走角色卡 rules 兜底，并把缺哪几项写出来。
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from core import factory
from core.openai_provider import OpenAIProvider


@pytest.fixture
def clean_llm(tmp_store):
    """每个用例前后都清掉页面设置与落盘文件（不污染别的用例）。"""
    import server

    server._LLM_RUNTIME.clear()
    path = server.llm_config_path()
    if path.exists():
        path.unlink()
    yield server
    server._LLM_RUNTIME.clear()
    if path.exists():
        path.unlink()


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


# ── 页面改配置：立即生效 + 落盘保留 ───────────────────────

def test_apply_llm_settings_switches_brain(clean_llm, monkeypatch, tmp_store):
    from core import config

    server = clean_llm
    monkeypatch.setattr(config, "API_KEY", "sk-test")
    holder: dict = {"client": None}

    status = server.apply_llm_settings(
        {"model": "qwen-plus", "base_url": "https://dash.example/v1", "reasoning_effort": "high"},
        holder)

    assert status["ready"] is True and status["model"] == "qwen-plus"
    assert status["reasoning_effort"] == "high"
    assert holder["client"] is not None                     # 立即换脑
    assert holder["client"].provider.base_url == "https://dash.example/v1"
    assert holder["client"].model == "qwen-plus"

    saved = json.loads((tmp_store / "llm_config.json").read_text("utf-8"))
    assert saved["model"] == "qwen-plus" and saved["reasoning_effort"] == "high"


def test_settings_survive_runtime_clear(clean_llm, monkeypatch):
    """用户要求：页面改的值下次启动还在 —— 清掉内存（模拟重启）后应来自文件。"""
    from core import config

    server = clean_llm
    monkeypatch.setattr(config, "API_KEY", "sk-test")
    server.apply_llm_settings({"model": "qwen-plus", "base_url": "https://dash.example/v1"})

    assert server.llm_config_status()["origin"] == "runtime"
    server._LLM_RUNTIME.clear()                             # 模拟重启
    status = server.llm_config_status()

    assert status["origin"] == "file"
    assert status["model"] == "qwen-plus"


def test_thinking_unsupported_sends_nothing(clean_llm, monkeypatch):
    """不支持深度思考的模型 → 实际一个思考参数都不发（= 安全的关闭）。"""
    server = clean_llm
    server.apply_llm_settings({"reasoning_effort": "high", "thinking_unsupported": True})

    assert server.reasoning_effort() == "high"              # 页面上仍显示用户的选择
    assert server.thinking_effort() is None                 # 但下发时一个都不发


def test_apply_rejects_bad_effort(clean_llm):
    with pytest.raises(ValueError):
        clean_llm.apply_llm_settings({"reasoning_effort": "超强"})


def test_console_llm_endpoints(tmp_store, write_persona, make_app_client, clean_llm, monkeypatch):
    from core import config

    monkeypatch.setattr(config, "API_KEY", "sk-test")
    client = make_app_client()

    ok = client.put("/api/llm", json={
        "model": "qwen-plus", "base_url": "https://dash.example/v1", "reasoning_effort": "low"})
    assert ok.status_code == 200
    assert ok.json()["model"] == "qwen-plus"
    assert ok.json()["reasoning_effort"] == "low"

    bad = client.put("/api/llm", json={"reasoning_effort": "超强"})
    assert bad.status_code == 400

    got = client.get("/api/llm").json()
    assert got["model"] == "qwen-plus" and got["origin"] == "runtime"

    assert client.delete("/api/llm").status_code == 200
    assert client.get("/api/llm").json()["origin"] == "env"


# ── key 安全（业界共识：敏感分离 / 掩码不回原文 / 不进仓库）──

def test_mask_secret_keeps_head_tail():
    import server

    assert server.mask_secret("") == ""
    assert server.mask_secret("short") == "sh…"
    assert server.mask_secret("sk-1234567890abcdef") == "sk-…cdef"


def test_scrub_removes_key_from_text(monkeypatch):
    import server
    from core import config

    monkeypatch.setattr(config, "API_KEY", "sk-SUPERSECRET123456")
    assert "SUPERSECRET" not in server.scrub("失败: Bearer sk-SUPERSECRET123456 无效")
    assert server.scrub("普通错误") == "普通错误"


def test_api_key_status_never_returns_raw(monkeypatch):
    import server
    from core import config

    monkeypatch.setattr(config, "API_KEY", "sk-SUPERSECRET123456")
    status = server.api_key_status()

    assert status["present"] is True
    assert status["masked"] != "sk-SUPERSECRET123456"
    assert "SUPERSECRET" not in json.dumps(status)


def test_apply_rejects_api_key_field(clean_llm):
    """护栏：收到 key 类字段要响亮拒绝，不能静默忽略。"""
    with pytest.raises(ValueError):
        clean_llm.apply_llm_settings({"api_key": "sk-xxx"})


def test_saved_llm_file_has_no_secret(clean_llm, monkeypatch, tmp_store):
    from core import config

    monkeypatch.setattr(config, "API_KEY", "sk-SUPERSECRET123456")
    clean_llm.apply_llm_settings({"model": "qwen-plus", "base_url": "https://dash.example/v1"})

    raw = (tmp_store / "llm_config.json").read_text("utf-8")
    assert "SUPERSECRET" not in raw
    assert set(json.loads(raw)) == set(clean_llm._LLM_FIELDS)


def test_endpoints_never_echo_key(tmp_store, write_persona, make_app_client, clean_llm,
                                  monkeypatch):
    from core import config

    monkeypatch.setattr(config, "API_KEY", "sk-SUPERSECRET123456")
    client = make_app_client()

    for path in ("/api/state", "/api/llm"):
        assert "SUPERSECRET" not in client.get(path).text

    bad = client.put("/api/llm", json={"api_key": "sk-xxx"})
    assert bad.status_code == 400
    assert "key 不在这里改" in bad.json()["detail"]


def test_gitignore_covers_secret_files():
    """防回归：这几个文件绝不能进仓库（以后误删 .gitignore 行会红）。"""
    text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text("utf-8")
    for name in ("api_key.txt", "store/"):
        assert name in text


# ── api_key.txt 权限：只检测，不自动改 ────────────────────

def test_perm_check_skips_non_windows(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server.os, "name", "posix")
    target = tmp_path / "api_key.txt"
    target.write_text("sk-x", encoding="utf-8")

    assert server.scan_key_file_perm(target) is None


def test_perm_check_skips_missing_file(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server.os, "name", "nt")
    assert server.scan_key_file_perm(tmp_path / "不存在.txt") is None


def test_perm_check_detects_world_readable(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "me")
    target = tmp_path / "api_key.txt"
    target.write_text("sk-x", encoding="utf-8")
    out = f"{target} BUILTIN\\Users:(R)\r\n NT AUTHORITY\\SYSTEM:(F)\r\n"
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0))

    hint = server.scan_key_file_perm(target)

    assert hint is not None
    assert "icacls" in hint and str(target) in hint
    assert "me:(R,W)" in hint                      # 给的命令是可直接复制的
    assert "不会自动改" in hint


def test_perm_check_quiet_when_private(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server.os, "name", "nt")
    target = tmp_path / "api_key.txt"
    target.write_text("sk-x", encoding="utf-8")
    out = f"{target} NT AUTHORITY\\SYSTEM:(F)\r\n DESKTOP\\me:(F)\r\n"
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0))

    assert server.scan_key_file_perm(target) is None


def test_perm_check_silent_when_icacls_fails(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server.os, "name", "nt")
    target = tmp_path / "api_key.txt"
    target.write_text("sk-x", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("icacls 不存在")

    monkeypatch.setattr(server.subprocess, "run", boom)

    assert server.scan_key_file_perm(target) is None
