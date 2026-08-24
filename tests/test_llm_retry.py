"""
NPC_LLM_RETRY — 网关抖动重试（2026-08-25）。

家规对照:
  - 代码默认关（NPC_LLM_RETRY 未设/≠1 → 零行为变化）
  - 只救瞬时病: 超时/连接断/限流(429)/5xx；参数错(4xx)立刻原样抛
  - 节奏可调 NPC_LLM_RETRY_DELAYS="a,b"，默认 [2.0, 6.0]（最坏 +8s < talk 60s 预算）
测试全程假时钟: time.sleep 被 monkeypatch，不真等待。
"""
import pytest

from agent.llm import client as C
from agent.llm.client import LLMClient


# ── 假件 ──────────────────────────────────────────────

class TimeoutBoom(Exception):
    pass


class RateLimited(Exception):
    status_code = 429


class ServerDown(Exception):
    status_code = 503


class BadRequest(Exception):
    status_code = 400


class FlakyProvider:
    """前 failures 次抛 exc，之后成功。记录调用次数/kwargs/sleep 由外部注入。"""

    def __init__(self, failures: int, exc: Exception):
        self.failures = failures
        self.exc = exc
        self.calls = 0
        self.kwargs = None
        self.model_name = "fake"

    def chat(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self.calls <= self.failures:
            raise self.exc
        return C.LLMResponse("ok", [], "stop", "fake", {})


@pytest.fixture
def fast_sleep(monkeypatch):
    """把模块内 time.sleep 换成记账器 — 测试零真实等待。"""
    sleeps: list = []
    monkeypatch.setattr(C.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _client(provider) -> LLMClient:
    return LLMClient(provider=provider)


MSGS = [{"role": "user", "content": "hi"}]


# ── 用例 ──────────────────────────────────────────────

def test_default_off_zero_behavior_change(monkeypatch):
    """家规验收: 不设开关 → 行为与旧版逐字节一致（失败即抛，不补试）。"""
    monkeypatch.delenv("NPC_LLM_RETRY", raising=False)
    p = FlakyProvider(1, TimeoutBoom("gateway sneezed"))
    with pytest.raises(TimeoutBoom):
        _client(p).chat(MSGS)
    assert p.calls == 1


def test_retry_rescues_transient_timeout(monkeypatch, fast_sleep):
    monkeypatch.setenv("NPC_LLM_RETRY", "1")
    p = FlakyProvider(1, TimeoutBoom("blip"))
    resp = _client(p).chat(MSGS)
    assert resp.content == "ok" and p.calls == 2
    assert fast_sleep == [2.0]          # 默认第一档等待


def test_retry_exhausts_then_raises_original(monkeypatch, fast_sleep):
    monkeypatch.setenv("NPC_LLM_RETRY", "1")
    p = FlakyProvider(99, ServerDown())          # 永远失败
    with pytest.raises(ServerDown):
        _client(p).chat(MSGS)
    assert p.calls == 3                           # 首发 + 2 次补试
    assert fast_sleep == [2.0, 6.0]               # 默认节奏全部用完


def test_rate_limit_429_is_retryable(monkeypatch, fast_sleep):
    monkeypatch.setenv("NPC_LLM_RETRY", "1")
    p = FlakyProvider(1, RateLimited())
    assert _client(p).chat(MSGS).content == "ok"
    assert p.calls == 2


def test_bad_request_never_retried(monkeypatch, fast_sleep):
    """4xx 是我们的错不是网关的错 — 盲试纯烧预算。"""
    monkeypatch.setenv("NPC_LLM_RETRY", "1")
    p = FlakyProvider(1, BadRequest())
    with pytest.raises(BadRequest):
        _client(p).chat(MSGS)
    assert p.calls == 1 and fast_sleep == []


def test_custom_delays_env(monkeypatch, fast_sleep):
    monkeypatch.setenv("NPC_LLM_RETRY", "1")
    monkeypatch.setenv("NPC_LLM_RETRY_DELAYS", "0.5")
    p = FlakyProvider(1, TimeoutBoom("x"))
    _client(p).chat(MSGS)
    assert fast_sleep == [0.5]
    p2 = FlakyProvider(99, TimeoutBoom("x"))
    with pytest.raises(TimeoutBoom):
        _client(p2).chat(MSGS)
    assert fast_sleep == [0.5, 0.5]              # 只有自定义的 1 档可用


def test_kwargs_pass_through_untouched(monkeypatch, fast_sleep):
    """包装层不得偷改/丢参数（reasoning_effort 等必须原样到 provider）。"""
    monkeypatch.setenv("NPC_LLM_RETRY", "1")
    p = FlakyProvider(0, None)
    _client(p).chat(MSGS, reasoning_effort="low", response_format={"type": "json_object"})
    assert p.kwargs["reasoning_effort"] == "low"
    assert p.kwargs["response_format"] == {"type": "json_object"}
    assert p.kwargs["messages"] == MSGS
