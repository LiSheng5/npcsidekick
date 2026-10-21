"""摘要移出关键路径（任务 6）测试 —— 回合结束后台触发 maybe_summarize。

要点：
  · build_history 不再同步跑摘要 LLM（turn.py 传 summarize=False）——
    第一个 delta 不等摘要；摘要在 append_turn 之后后台触发
  · 同一 NPC 同时只允许一个摘要在跑
  · 摘要文件原子写（先 .tmp 再 os.replace），不留半截文件

测试直接驱动 turn.stream_turn（asyncio.run），能观察「首字到达时摘要还没跑完」。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

import chatlog
import turn
from core.client import LLMClient, LLMResponse
from core.types import StreamChunk


@pytest.fixture(autouse=True)
def _clean_summary_state():
    """清掉后台摘要的模块级状态，避免测试间互相污染。"""
    turn._SUMMARIZING.clear()
    turn._SUMMARY_TASKS.clear()
    yield
    turn._SUMMARIZING.clear()
    turn._SUMMARY_TASKS.clear()


class SummaryProvider:
    """stream 立刻回台词；chat（摘要走这条）故意慢 —— 模拟真实摘要 LLM 延迟。

    - `block=True`：chat 卡在 `release` 事件上（要调用方 set 才继续）——
      制造「上一个摘要还没跑完」的确定性时序，不靠 sleep 赌时间
    - `chat_done` / `chat_calls` / `max_active` 用来断言摘要时序与并发
    """

    model_name = "slow-fake"

    def __init__(self, summary_text: str = "前情摘要", delay: float = 0.3,
                 block: bool = False):
        self.summary_text = summary_text
        self.delay = delay
        self.block = block
        self.release = threading.Event()
        self.chat_calls: list = []
        self.chat_done = False
        self.active = 0
        self.max_active = 0

    def chat(self, messages, **kwargs):
        self.chat_calls.append(messages)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.block:
            self.release.wait(timeout=5)
        else:
            time.sleep(self.delay)
        self.active -= 1
        self.chat_done = True
        return LLMResponse(content=self.summary_text, tool_calls=[],
                           finish_reason="stop", model=self.model_name, usage=None)

    async def stream(self, messages, **kwargs):
        yield StreamChunk(content="嗯。", model=self.model_name)
        yield StreamChunk(finish_reason="stop", model=self.model_name)


def _persona() -> dict:
    return {"id": "shu", "name": "叔", "identity": "部落的老猎手",
            "personality": "寡言直接", "speech_style": "短句，不废话",
            "rules": {"replies": {}, "fallback": "嗯，火塘边坐着说。"}}


def _parse(frame: str) -> dict:
    return json.loads(frame.split("data: ", 1)[1].strip())


async def _run_turn(llm, message: str, store_dir, npc_id: str = "shu"):
    """跑完一回合 → (帧列表, 首个 delta 到达时摘要 chat 是否已跑完)。"""
    frames = []
    chat_done_at_first_delta = None
    async for raw in turn.stream_turn(llm=LLMClient(provider=llm), npc_id=npc_id,
                                      message=message, persona=_persona(),
                                      lock=asyncio.Lock(), store_dir=store_dir,
                                      execute_timeout=lambda: 5.0):
        frames.append(_parse(raw))
        if chat_done_at_first_delta is None and frames[-1]["type"] == "delta":
            chat_done_at_first_delta = llm.chat_done
    return frames, chat_done_at_first_delta


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _over_threshold_chatlog(npc_id: str, store_dir, rounds: int = 5) -> None:
    """先造一段够压的旧聊天记录（配合 `_ratio_env` 的低压缩阈值）。"""
    for i in range(rounds):
        chatlog.append_turn(npc_id, "问" * 20 + str(i), "答" * 20 + str(i),
                            store_dir=store_dir)


def _ratio_env(monkeypatch) -> None:
    """窗口 1000 × 比例 0.01 → 压缩阈值 10：正常回合的提示都会越线。

    硬上限放开、保留 1 轮，只让「提示越线 → 触发后台摘要」这一条路径生效。
    """
    monkeypatch.setenv("NPC_CONTEXT_WINDOW", "1000")
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "0.01")
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "1")
    monkeypatch.setenv("NPC_BUDGET_HISTORY", "100000")


# ── 首字不等摘要，摘要最终被保存 ──────────────────────────

def test_first_delta_before_summary_completes_and_summary_saved(tmp_store, monkeypatch):
    """假 LLM 的 chat() 故意慢：第一个 delta 在摘要完成之前就到了；摘要最终被保存。"""
    _ratio_env(monkeypatch)
    _over_threshold_chatlog("shu", tmp_store)
    provider = SummaryProvider(summary_text="前情：聊过问的事。", delay=0.3)

    async def main():
        frames, chat_done_at_first_delta = await _run_turn(provider, "在吗", tmp_store)
        saved = await _wait_for(
            lambda: chatlog.summary_path("shu", store_dir=tmp_store).exists())
        return frames, chat_done_at_first_delta, saved

    frames, chat_done_at_first_delta, saved = asyncio.run(main())

    assert chat_done_at_first_delta is False      # 首字到达时摘要还没跑完（旧同步行为会是 True）
    assert any(f["type"] == "delta" for f in frames)
    assert frames[-1]["type"] == "done"
    assert saved                                  # 摘要最终被保存（后台跑完）
    assert chatlog.load_summary("shu", store_dir=tmp_store)["summary"] == "前情：聊过问的事。"
    assert list(tmp_store.glob("*.tmp")) == []    # 原子写，不残留 .tmp


def test_build_history_without_summarize_does_not_call_llm(tmp_store, make_provider,
                                                           monkeypatch):
    """turn.py 走的 summarize=False：拼历史不惊动摘要 LLM（默认 True 保持旧行为）。"""
    _ratio_env(monkeypatch)
    _over_threshold_chatlog("shu", tmp_store)
    provider = make_provider()

    chatlog.build_history("shu", provider, token_budget=1000,
                          store_dir=tmp_store, summarize=False)

    assert provider.chat_calls == []              # 回合内不再同步跑摘要
    assert not chatlog.summary_path("shu", store_dir=tmp_store).exists()


# ── 同一 NPC 不并发摘要 ──────────────────────────────────

def test_no_concurrent_summaries_for_same_npc(tmp_store, monkeypatch):
    """连续两个回合都触发摘要条件：同一 NPC 不会并发跑两个摘要。"""
    _ratio_env(monkeypatch)
    _over_threshold_chatlog("shu", tmp_store)
    provider = SummaryProvider(summary_text="前情摘要", block=True)

    async def main():
        for _ in range(2):                        # 连续两回合，都满足摘要条件
            await _run_turn(provider, "在吗", tmp_store)
        provider.release.set()                    # 放行第一回合启动的那次摘要
        return await _wait_for(
            lambda: chatlog.summary_path("shu", store_dir=tmp_store).exists())

    saved = asyncio.run(main())

    assert len(provider.chat_calls) == 1          # 第二回合没再起一个摘要
    assert provider.max_active == 1               # 从未并发
    assert saved


# ── 摘要文件原子写 ───────────────────────────────────────

def test_save_summary_atomic_no_partial_file(tmp_store):
    """_save_summary 先写 .tmp 再替换：最终文件合法、.tmp 不残留。"""
    chatlog._save_summary("shu", "摘要内容", 4, store_dir=tmp_store)

    path = chatlog.summary_path("shu", store_dir=tmp_store)
    data = json.loads(path.read_text("utf-8"))
    assert data["summary"] == "摘要内容" and data["covered"] == 4
    assert data["updated_at"] > 0
    assert list(tmp_store.glob("*.tmp")) == []    # 不留半截文件
