"""世界自治档位闸门测试（2026-09-17）。

覆盖:
  - autonomy_mode 声明读取（未声明/非法 → free 零回归）
  - _tick_loop 闸门: game+无客户端冻结世界推进/落盘, game+有客户端推进, 超时重新冻结
  - 单帧异常不杀循环（既有契约）
  - features.flags 观测: autonomy(字符串) / frozen(bool), 二者都不进决策类布尔开关表
"""
import asyncio
import time
from contextlib import suppress

import pytest
from fastapi.testclient import TestClient

from npc import server as srv
from npc.npc import NPC
from npc.server import create_npc_server
from npc.world import autonomy_mode


# ── 1. autonomy_mode 纯函数（声明驱动，零回归） ──────────────────────────────

class TestAutonomyMode:
    def test_undeclared_is_free(self):
        # 未声明 = free（= 旧行为，零回归）
        assert autonomy_mode({}) == "free"
        assert autonomy_mode({"actors": {}}) == "free"
        assert autonomy_mode({"_autonomy": None}) == "free"

    def test_explicit_free(self):
        assert autonomy_mode({"_autonomy": "free"}) == "free"

    def test_game_mode(self):
        assert autonomy_mode({"_autonomy": "game"}) == "game"

    def test_invalid_falls_back_to_free(self):
        # 任何非法声明值 → free（不破坏旧世界）
        assert autonomy_mode({"_autonomy": "bogus"}) == "free"
        assert autonomy_mode({"_autonomy": "GAME"}) == "free"
        assert autonomy_mode({"_autonomy": 123}) == "free"
        assert autonomy_mode({"_autonomy": ["game"]}) == "free"


# ── 2. 世界自治闸门（_tick_loop 行为） ─────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_presence():
    # 默认"无客户端在场"态: 初始 0.0 = 远超 TTL → 模拟游戏端未开(应冻结)
    srv._last_client_seen = 0.0
    srv._tick_frozen = False
    yield
    srv._last_client_seen = 0.0
    srv._tick_frozen = False


def _fake_world(autonomy):
    return {"_autonomy": autonomy, "_tick": 0, "_prev_hour": None}


async def _drive_loop(world, npcs, n_ticks, interval=0.005, *, monkeypatch):
    """跑 _tick_loop n_ticks 帧后取消, 返回 pump/tick 调用次数。

    把"世界推进"相关函数换成本地计数器; "记忆维护"相关惰性机制在少量帧内不会触发
    (反思=20 tick / 合并=60 tick, 黎明需跨 5→6 点), 故不影响断言。管家/黎明/僵尸回收
    一并打桩, 让测试不依赖真实世界与 LLM。
    """
    calls = {"pump": 0, "tick": 0}

    def fake_pump(w):
        calls["pump"] += 1

    def fake_tick(w, n, rng):
        calls["tick"] += 1
        return {}

    monkeypatch.setattr(srv, "pump_task_dispatch", fake_pump)
    monkeypatch.setattr(srv, "tick_round", fake_tick)
    monkeypatch.setattr(srv, "_save_on_transitions", lambda n, e: None)
    monkeypatch.setattr(srv._hk, "enabled", lambda: False)   # 跳过管家/黎明(需真实世界)
    monkeypatch.setattr(srv._taskloop.LEDGER, "reap_zombies", lambda: None)
    monkeypatch.setattr(srv, "_tick_interval", lambda: interval)

    task = asyncio.create_task(srv._tick_loop(world, npcs))
    await asyncio.sleep(interval * n_ticks + 0.03)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return calls


class TestTickLoopGate:
    async def test_game_no_client_freezes_world(self, monkeypatch):
        # game 档 + 无客户端在场(超时) → 世界推进与落盘被跳过
        world = _fake_world("game")
        calls = await _drive_loop(world, {}, 6, monkeypatch=monkeypatch)
        assert calls["pump"] == 0
        assert calls["tick"] == 0

    async def test_game_with_client_advances(self, monkeypatch):
        # game 档 + 客户端刚请求过(在场) → 正常推进
        world = _fake_world("game")
        srv._last_client_seen = time.monotonic()
        calls = await _drive_loop(world, {}, 6, monkeypatch=monkeypatch)
        assert calls["pump"] > 0
        assert calls["tick"] > 0

    async def test_undeclared_free_advances(self, monkeypatch):
        # 未声明 _autonomy → free → 即便无客户端也永远推进（零回归）
        world = _fake_world(None)
        srv._last_client_seen = 0.0
        calls = await _drive_loop(world, {}, 6, monkeypatch=monkeypatch)
        assert calls["pump"] > 0
        assert calls["tick"] > 0

    async def test_client_timeout_refreezes(self, monkeypatch):
        # 在场推进 → 客户端离开(超时) → 重新冻结
        world = _fake_world("game")
        srv._last_client_seen = time.monotonic()
        calls1 = await _drive_loop(world, {}, 5, monkeypatch=monkeypatch)
        assert calls1["pump"] > 0                      # 在场 → 推进
        srv._last_client_seen = 0.0                    # 客户端离开(远超 TTL)
        calls2 = await _drive_loop(world, {}, 5, monkeypatch=monkeypatch)
        assert calls2["pump"] == 0                     # 超时 → 重新冻结

    async def test_loop_survives_tick_exception(self, monkeypatch):
        # 单帧异常不能杀循环(既有契约)
        world = _fake_world("free")
        srv._last_client_seen = time.monotonic()
        calls = {"tick": 0}

        def boom(w, n, rng):
            calls["tick"] += 1
            if calls["tick"] == 1:
                raise RuntimeError("boom")
            return {}

        monkeypatch.setattr(srv, "pump_task_dispatch", lambda w: None)
        monkeypatch.setattr(srv, "tick_round", boom)
        monkeypatch.setattr(srv, "_save_on_transitions", lambda n, e: None)
        monkeypatch.setattr(srv._hk, "enabled", lambda: False)
        monkeypatch.setattr(srv._taskloop.LEDGER, "reap_zombies", lambda: None)
        monkeypatch.setattr(srv, "_tick_interval", lambda: 0.005)

        task = asyncio.create_task(srv._tick_loop(world, {}))
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        # 异常后循环仍存活: tick 被调用了不止一次
        assert calls["tick"] > 1


# ── 3. 可观测 + middleware ────────────────────────────────────────────────

class TestFlagsAndMiddleware:
    def test_flags_present_for_game_world(self, tmp_path):
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        npc.world["_autonomy"] = "game"
        with TestClient(create_npc_server({"cang": npc})) as c:
            flags = c.get("/api/version").json()["features"]["flags"]
        # autonomy 是字符串(档位), frozen 是运行时态(bool)
        assert flags["autonomy"] == "game"
        assert isinstance(flags["frozen"], bool)
        # 二者都不应是"决策类布尔开关": 不进 test_server_console 的 _DECISION_FLAGS 表
        assert "autonomy" not in _DECISION_FLAG_NAMES
        # frozen 不入布尔开关表(它是运行时态, 非"默认关+选择加入"开关) —— 见 TestFlagsObservability

    def test_flags_free_default(self, tmp_path):
        # 未声明 _autonomy → free; frozen 默认 False
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        with TestClient(create_npc_server({"cang": npc})) as c:
            flags = c.get("/api/version").json()["features"]["flags"]
        assert flags["autonomy"] == "free"
        assert flags["frozen"] is False

    def test_middleware_marks_presence(self, tmp_path):
        # 任意请求经过 middleware 即刷新"最后请求时间"
        npc = NPC(store_dir=str(tmp_path))
        npc.use_llm = False
        npc.world["_autonomy"] = "game"
        srv._last_client_seen = 0.0
        with TestClient(create_npc_server({"cang": npc})) as c:
            c.get("/api/version")
            assert srv._last_client_seen > 0.0


# test_server_console.TestFlagsObservability._DECISION_FLAGS 的键名集合
# (确认 autonomy/frozen 不混入"决策类布尔开关"表 —— 那张表只验默认关/Live-read 的开关)。
_DECISION_FLAG_NAMES = {"goals", "lessons", "goal_relevance",
                        "memory_dedup", "safety_gate"}
