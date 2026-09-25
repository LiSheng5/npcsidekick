"""pytest 共享夹具。

- 把工程根插入 sys.path，使 `import core.*` / `import memory` 等可用
- 所有测试零网络：LLM 一律用 FakeProvider 注入
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base import ProviderProtocol          # noqa: E402
from core.client import LLMClient, LLMResponse  # noqa: E402
from core.types import StreamChunk              # noqa: E402


# ── 假 Provider ───────────────────────────────────────────

class FakeProvider(ProviderProtocol):
    """脚本化 Provider：按 turns 顺序回放，不碰网络。

    每个 turn 形如:
      {"text": "行啊，", "tool_calls": [{"name": "remember", "arguments": {...}}]}
    turns 用尽后回放 {"text": "嗯。"}。

    stream() 会把 text 切成 chunk_size 大小的多片；tool_call 的
    function_name / function_arguments 分两个 chunk 吐出，用于验证分片拼装。
    """

    def __init__(self, turns: Optional[List[dict]] = None, chunk_size: int = 2):
        self.turns = list(turns or [])
        self.chunk_size = max(1, chunk_size)
        self.chat_calls: List[dict] = []
        self.stream_calls: List[dict] = []
        self._used = 0

    @property
    def model_name(self) -> str:
        return "fake-model"

    def _next_turn(self) -> dict:
        if self._used < len(self.turns):
            turn = self.turns[self._used]
        else:
            turn = {"text": "嗯。"}
        self._used += 1
        return turn

    def _tool_calls(self, turn: dict):
        calls = []
        for i, tc in enumerate(turn.get("tool_calls") or []):
            calls.append(_FakeToolCall(f"call_{i + 1}", tc.get("name", ""), tc.get("arguments") or {}))
        return calls

    def chat(self, messages, tools=None, tool_choice="auto", temperature=None,
             max_tokens=None, response_format=None, reasoning_effort=None) -> LLMResponse:
        self.chat_calls.append({"messages": messages, "tools": tools,
                                "reasoning_effort": reasoning_effort})
        turn = self._next_turn()
        calls = self._tool_calls(turn)
        return LLMResponse(
            content=turn.get("text", ""),
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            model=self.model_name,
            usage=None,
        )

    async def stream(self, messages, tools=None, tool_choice="auto", temperature=None,
                     max_tokens=None, reasoning_effort=None) -> AsyncGenerator[StreamChunk, None]:
        self.stream_calls.append({"messages": messages, "tools": tools,
                                  "reasoning_effort": reasoning_effort})
        turn = self._next_turn()
        idx = 0
        text = turn.get("text", "")
        for i in range(0, len(text), self.chunk_size):
            idx += 1
            yield StreamChunk(content=text[i:i + self.chunk_size], model=self.model_name, index=idx)

        for i, tc in enumerate(turn.get("tool_calls") or []):
            args = json.dumps(tc.get("arguments") or {}, ensure_ascii=False)
            idx += 1
            yield StreamChunk(
                tool_call_delta={"index": i, "id": f"call_{i + 1}",
                                 "function_name": tc.get("name", ""), "function_arguments": None},
                model=self.model_name, index=idx)
            idx += 1
            yield StreamChunk(
                tool_call_delta={"index": i, "id": None,
                                 "function_name": None, "function_arguments": args},
                model=self.model_name, index=idx)

        idx += 1
        yield StreamChunk(finish_reason="tool_calls" if turn.get("tool_calls") else "stop",
                          model=self.model_name, index=idx)


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict):
        self.id = call_id
        self.function = _FakeFunction(name, json.dumps(arguments, ensure_ascii=False))


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class BoomProvider(FakeProvider):
    """每次调用都抛异常 —— 用于验证 LLM 不可用时的降级。"""

    def chat(self, *a, **kw):
        raise RuntimeError("boom")

    async def stream(self, *a, **kw):
        raise RuntimeError("boom")
        yield  # pragma: no cover


# ── 夹具 ─────────────────────────────────────────────────

@pytest.fixture
def make_provider():
    """FakeProvider 工厂：make_provider(turns=[...])。"""
    def _make(turns=None, chunk_size: int = 2):
        return FakeProvider(turns=turns, chunk_size=chunk_size)
    return _make


@pytest.fixture
def make_client():
    """LLMClient 工厂（provider 注入，零网络）。"""
    def _make(provider=None):
        return LLMClient(provider=provider or FakeProvider())
    return _make


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """把 memory / chatlog 的 store 指到临时目录。"""
    import memory

    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(memory, "STORE_DIR", store)
    try:
        import chatlog
    except ImportError:
        pass
    else:
        monkeypatch.setattr(chatlog, "STORE_DIR", store)
    return store


@pytest.fixture
def persona_dir(tmp_path, monkeypatch):
    """把 server 的角色卡目录指到临时目录。"""
    import server

    d = tmp_path / "personas"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PERSONA_DIR", d)
    return d


@pytest.fixture
def avatar_dir(tmp_path, monkeypatch):
    """把 server 的头像目录指到临时目录（create_app 会自己 mkdir）。"""
    import server

    d = tmp_path / "avatars"
    monkeypatch.setattr(server, "AVATAR_DIR", d)
    return d


@pytest.fixture
def write_persona(persona_dir):
    """往临时角色卡目录写一张角色卡 JSON。"""
    def _write(npc_id: str = "cang", **overrides) -> Dict[str, Any]:
        data = {
            "id": npc_id,
            "name": "苍",
            "identity": "部落的老猎手",
            "personality": "寡言直接",
            "speech_style": "短句，不废话",
            "rules": {"replies": {"狩猎": "别追跑得快的。"}, "fallback": "嗯，火塘边坐着说。"},
        }
        data.update(overrides)
        (persona_dir / f"{npc_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    return _write


@pytest.fixture
def make_app_client(tmp_store, persona_dir, avatar_dir):
    """FastAPI TestClient 工厂：make_app_client(provider=..., **state)。

    avatar_dir 也让 make_app_client 依赖 —— 保证任何测试都不会写到工程里真实的 avatars/。
    """
    from fastapi.testclient import TestClient

    import server

    def _make(provider=None, **state_kwargs):
        app = server.create_app(llm_client=LLMClient(provider=provider or FakeProvider()),
                                **state_kwargs)
        return TestClient(app)
    return _make
