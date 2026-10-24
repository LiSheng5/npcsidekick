"""pytest 共享夹具与共享测试工具。

- 把工程根插入 sys.path，使 `import core.*` / `import memory` 等可用
- 所有测试零外网、允许回环：LLM 一律用 FakeProvider 注入；
  少数测试会起本地回环 HTTP 服务器（见 test_server 的真实往返用例）
- 共享常量 / SSE 解析助手 / FakeExecutor 统一放这里 —— 测试模块之间不要互相 import
"""
from __future__ import annotations

import asyncio
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
      {"text": "行啊，", "reasoning": "先想想", "tool_calls": [{"name": "remember", "arguments": {...}}]}
    turns 用尽后回放 {"text": "嗯。"}。

    stream() 会把 text 切成 chunk_size 大小的多片；tool_call 的
    function_name / function_arguments 分两个 chunk 吐出，用于验证分片拼装。
    `reasoning` = 思考增量（模拟 DeepSeek 的 reasoning_content），也会按片吐出。
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
            reasoning=turn.get("reasoning", ""),
        )

    async def stream(self, messages, tools=None, tool_choice="auto", temperature=None,
                     max_tokens=None, reasoning_effort=None) -> AsyncGenerator[StreamChunk, None]:
        self.stream_calls.append({"messages": messages, "tools": tools,
                                  "reasoning_effort": reasoning_effort})
        turn = self._next_turn()
        idx = 0
        reasoning = turn.get("reasoning", "")
        for i in range(0, len(reasoning), self.chunk_size):
            idx += 1
            yield StreamChunk(reasoning=reasoning[i:i + self.chunk_size],
                              model=self.model_name, index=idx)

        text = turn.get("text", "")
        for i in range(0, len(text), self.chunk_size):
            idx += 1
            yield StreamChunk(content=text[i:i + self.chunk_size], model=self.model_name, index=idx)

        for i, tc in enumerate(turn.get("tool_calls") or []):
            # raw_arguments 用于模拟"模型吐了坏 JSON"的调用（不经过 json.dumps）
            args = tc.get("raw_arguments")
            if args is None:
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


# ── 共享常量 / SSE 助手 / 假执行器（所有测试模块共用，别再各自复制）──

# mod 报上来的动作声明（cook / goto / chat）与标准 capabilities 载荷
ACTIONS: List[Dict[str, Any]] = [
    {"name": "cook", "desc": "用厨房做饭", "params": {"dish": "菜名(字符串)"}},
    {"name": "goto", "desc": "走到某地", "params": {"place": "地点名(字符串)"}},
    {"name": "chat", "desc": "主动找某人说话",
     "params": {"target": "对象名", "topic": "话题"}},
]
CAPS: Dict[str, Any] = {
    "mod": "mygame",
    "execute_url": "http://127.0.0.1:8766/execute",
    "actions": list(ACTIONS),
}


def frames(response) -> list:
    """把 SSE 响应体拆成帧列表。"""
    out = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: "):]))
    return out


def kinds(response) -> list:
    """SSE 帧的 type 序列。"""
    return [f["type"] for f in frames(response)]


def text_of(response) -> str:
    """SSE 帧里 delta 台词的拼接。"""
    return "".join(f.get("text", "") for f in frames(response) if f["type"] == "delta")


class FakeExecutor:
    """假执行器：记录每次调用，按脚本返回（results 队列优先）或抛异常。"""

    def __init__(self, result=None, results=None, error=None, delay=0.0):
        self.calls = []
        self.results = list(results) if results else None
        self.result = result or {"ok": True, "status": "done", "note": "面煮好了"}
        self.error = error
        self.delay = delay

    async def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return dict(self.result)


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
def paths(tmp_path):
    """注入用的运行时数据目录（store / personas / avatars 全在 tmp 下，零 monkeypatch）。"""
    from core.paths import Paths

    return Paths(root=tmp_path)


@pytest.fixture
def tmp_store(paths):
    """memory / chatlog / llm_config 的运行时数据目录（显式传 store_dir 用）。"""
    store = paths.store_dir
    store.mkdir(parents=True, exist_ok=True)
    return store


@pytest.fixture
def persona_dir(paths):
    d = paths.persona_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def avatar_dir(paths):
    """create_app 会自己 mkdir；测试都经由 make_app_client 注入，不会碰真实 avatars/。"""
    return paths.avatar_dir


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
def make_app_client(paths):
    """FastAPI TestClient 工厂：make_app_client(provider=..., **state)。

    运行时数据目录（store / personas / avatars）全部经 paths 注入到 tmp 下 ——
    保证任何测试都不会写到工程里真实的 store/ 与 avatars/。
    """
    from fastapi.testclient import TestClient

    import server

    def _make(provider=None, **state_kwargs):
        app = server.create_app(llm_client=LLMClient(provider=provider or FakeProvider()),
                                paths=paths, **state_kwargs)
        return TestClient(app)
    return _make
