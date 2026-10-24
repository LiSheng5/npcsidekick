"""server.py 测试 —— /api/state、/api/npcs、大脑装配（build_default_client）、
事件循环不被同步摘要堵住。"""

from __future__ import annotations

import memory
from conftest import CAPS

# ── 状态查询（/api/state、/api/npcs）─────────────────────

def test_state_endpoint(tmp_store, write_persona, make_app_client, monkeypatch):
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "0.5")
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "2")
    write_persona("cang")
    client = make_app_client()
    client.post("/api/capabilities", json=CAPS)

    state = client.get("/api/state").json()

    assert state["model"] == "fake-model"
    assert state["store_dir"] == str(tmp_store)
    assert state["npcs"] == 1
    assert state["mods"]["mygame"]["online"] is True
    assert state["heartbeat_timeout_s"] == 60.0
    # 双端共享值从这里下发（前端不再各抄一份字面量）
    assert state["avatar_max_bytes"] == 2 * 1024 * 1024
    assert "删卡不该连带删记忆" in state["delete_kept_note"]
    # 摘要阈值随 env 现读下发（前端聊天页文案按它现算，不写死 40%/3 轮）
    assert state["summary_ratio"] == 0.5
    assert state["summary_keep_recent"] == 2

def test_npcs_endpoint(tmp_store, write_persona, make_app_client, make_provider):
    write_persona("cang")
    write_persona("ali", name="阿黎")
    memory.add_entry("cang", "一条记忆", store_dir=tmp_store)
    client = make_app_client(provider=make_provider(turns=[{"text": "嗯。"}]))
    client.post("/api/talk", json={"npc_id": "cang", "message": "在吗"})

    data = client.get("/api/npcs").json()["npcs"]

    by_id = {n["npc_id"]: n for n in data}
    assert set(by_id) == {"cang", "ali"}
    assert by_id["cang"]["memory_entries"] == 1
    assert by_id["cang"]["chat_messages"] == 2
    assert by_id["cang"]["last_activity"] > 0
    assert by_id["ali"]["chat_messages"] == 0


# ── 大脑装配（build_default_client 跟随用户配置）──────────

def test_build_default_client_follows_user_config(tmp_store, monkeypatch):
    """三件套由用户自配：缺模型名/端点 → None（不偷偷用默认厂商）；配齐 → 用配置的那套。"""
    import server
    from core import config

    monkeypatch.setattr(config, "api_key", lambda: "sk-test")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("NPC_MODEL", raising=False)
    monkeypatch.delenv("NPC_BASE_URL", raising=False)
    assert server.build_default_client(store_dir=tmp_store) is None

    monkeypatch.setenv("AGENT_MODEL", "qwen-plus")
    monkeypatch.setenv("NPC_BASE_URL", "https://dashscope.example/v1")
    client = server.build_default_client(store_dir=tmp_store)
    assert client.model == "qwen-plus"
    assert client.provider.base_url == "https://dashscope.example/v1"

    # 换模型名不改端点：显式配的端点永远优先，不被厂商推断覆盖
    monkeypatch.setenv("AGENT_MODEL", "deepseek-v4-pro")
    assert server.build_default_client(
        store_dir=tmp_store).provider.base_url == "https://dashscope.example/v1"


# ── 事件循环不被同步 IO 堵住（回合里的阻塞调用走线程池）─────

def test_summary_llm_does_not_block_event_loop(tmp_store, paths, write_persona, monkeypatch):
    """滚动摘要的同步 LLM 请求必须走线程池。

    回归：build_history 以前在 SSE 生成器里直接调同步 llm.chat —— 摘要一变慢
    （几秒到几十秒），整个事件循环就被卡住，别的 NPC / 控制台全部无响应。
    确定性口径（不与墙钟赛跑）：摘要 chat 卡在 Event 上不放行时，事件循环必须
    还能立刻响应别的请求；放行后摘要落盘、回合正常收尾。
    """
    import asyncio
    import threading

    import httpx

    import chatlog
    import server
    from core.client import LLMClient, LLMResponse
    from core.types import StreamChunk

    monkeypatch.setenv("NPC_CONTEXT_WINDOW", "1000")
    monkeypatch.setenv("NPC_SUMMARY_RATIO", "0.01")        # 阈值 = 10：提示必越线
    monkeypatch.setenv("NPC_SUMMARY_KEEP_RECENT", "0")
    monkeypatch.setenv("NPC_BUDGET_HISTORY", "100000")
    write_persona("cang")
    chatlog.append_turn("cang", "你好世界", "嗯", store_dir=tmp_store)   # 可压的旧轮次 + 越线 → 必触发摘要

    class BlockedSummaryProvider:
        """stream 立刻回台词；chat（摘要走这条）卡在 Event 上，由测试放行。"""
        model_name = "blocked-fake"

        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def chat(self, messages, **kwargs):
            self.started.set()
            self.release.wait(timeout=5)
            return LLMResponse(content="（前情摘要）", tool_calls=[],
                               finish_reason="stop", model=self.model_name, usage=None)

        async def stream(self, messages, **kwargs):
            yield StreamChunk(content="在的。", model=self.model_name)
            yield StreamChunk(finish_reason="stop", model=self.model_name)

    provider = BlockedSummaryProvider()
    app = server.create_app(llm_client=LLMClient(provider=provider), paths=paths)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            talk = asyncio.create_task(
                ac.post("/api/talk", json={"npc_id": "cang", "message": "在吗"}))
            while not provider.started.is_set():      # 等后台摘要真的开始（线程池里）
                await asyncio.sleep(0.01)
            # 摘要还卡着（release 未放行）：事件循环必须还能响应别的请求
            probe = await ac.get("/api/state")
            assert probe.status_code == 200
            provider.release.set()
            assert (await talk).status_code == 200
            while not chatlog.summary_path("cang", store_dir=tmp_store).exists():
                await asyncio.sleep(0.01)             # 摘要最终落盘才收场

    asyncio.run(main())
