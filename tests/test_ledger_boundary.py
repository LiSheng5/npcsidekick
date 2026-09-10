"""任务回路账本边界锚: M2 划界(防双消费者竞态) + 僵尸 reap 免疫(防已完成被误判)。

背景（2026-08-25 大脑侧评审 B6）：玩家单经 ``NPC.book`` 落进 LEDGER 的同时也会
镜像到 ``npc.pending_task``。若文本执行引擎(scheduler)与外部消费者(mod)都去消费
pending_task，就会出现双份执行，且账本会把**已经干完**的活误判成僵尸、补写失败记忆。

该问题已在 M2 划界（``npc/scheduler.py:_protocol_owns_pending``）修复，但当时
**未留回归锚**。本文件钉住三件事，任何一条被改坏都应立刻变红：

  ① 门开（NPC_TASK_LOOP=1）：引擎不消费 pending_task —— 玩家单归外部消费者；
  ② 门关（默认）：引擎照旧消费 pending_task —— 旧石器等内置执行器世界零回归；
  ③ 已销账(completed)的任务对 reap_zombies **免疫**，不会被补写失败/推商议。

真实 GTA 联机路径的端到端另见 tests/test_taskloop_reinforce.py 的 task_client。
"""
import random
import time

from npc import taskloop as tl
from npc.npc import NPC
from npc.scheduler import _protocol_owns_pending, tick_round
from npc.world import default_world


def _npc(pid, routine, world, store_dir):
    persona = {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [],
        "rules": {"replies": {"好": "好的。"}, "fallback": "嗯。"},
        "routine": routine,
    }
    return NPC(persona=persona, world=world, store_dir=store_dir)


# ══ M2 划界：玩家单(pending_task)归属 ════════════════════════

class TestPendingOwnership:
    """玩家指令 > 自主日常；协议模式下玩家单归外部消费者，引擎不抢跑。"""

    def test_gate_flag_reads_live(self, monkeypatch):
        """划界开关现读可热切（家规），不是 import 时冻结的常量。"""
        monkeypatch.delenv("NPC_TASK_LOOP", raising=False)
        assert _protocol_owns_pending() is False      # 默认关
        monkeypatch.setenv("NPC_TASK_LOOP", "1")
        assert _protocol_owns_pending() is True       # 现读现切

    def test_protocol_mode_engine_does_not_steal_pending(self, monkeypatch, tmp_path):
        """门开：自主 tick 不消费 pending_task —— 防外部/内部双份执行。"""
        monkeypatch.setenv("NPC_TASK_LOOP", "1")
        world = default_world()
        npc = _npc("dingjia", None, world, str(tmp_path))
        npc.pending_task = {"action": "gather", "resource": "木材", "count": 1}

        tick_round(world, {"dingjia": npc}, rng=random.Random(1))

        assert npc.pending_task is not None           # 引擎没吃掉玩家的单

    def test_default_mode_engine_owns_pending(self, monkeypatch, tmp_path):
        """门关（默认）：引擎照旧接单执行 —— 内置执行器世界零回归。"""
        monkeypatch.delenv("NPC_TASK_LOOP", raising=False)
        world = default_world()
        npc = _npc("liluo", None, world, str(tmp_path))
        npc.pending_task = {"action": "gather", "resource": "木材", "count": 1}

        tick_round(world, {"liluo": npc}, rng=random.Random(1))

        assert npc.pending_task is None               # 接单后即取走（只执行一次）


# ══ 僵尸 reap：终态免疫 ═════════════════════════════════════

class TestZombieReap:
    """超时兜底只该碰「派发了但从没销账」的活；干完的不许被翻旧账。"""

    def test_settled_task_is_reap_immune(self):
        """已 completed 的任务即便伪装成陈年 dispatched，也不被 reap、不进商议。"""
        led = tl.TaskLedger()
        t = led.book("cang", "goto", desc="去河边")
        led.dispatch_view()                                   # booked → dispatched
        assert led.settle(t["task_id"], "completed")["state"] == "completed"

        led._tasks[t["task_id"]]["dispatched_at"] = time.time() - 10_000   # 伪装很久
        assert led.reap_zombies() == []                       # 终态 → 不碰
        assert led.discussions("cang") == []                  # 不补写失败商议
        assert led.stats()["by_state"] == {"completed": 1}

    def test_unsettled_dispatched_times_out(self):
        """真正没销账的 dispatched 超时 → failed(timeout) 并入商议（正向对照）。"""
        led = tl.TaskLedger()
        t = led.book("cang", "goto", desc="去河边")
        led.dispatch_view()
        led._tasks[t["task_id"]]["dispatched_at"] = time.time() - (tl.task_timeout_sec() + 5)

        assert led.reap_zombies() == [t["task_id"]]
        assert led._tasks[t["task_id"]]["state"] == "failed"
        assert led._tasks[t["task_id"]]["error"] == "timeout"
        assert len(led.discussions("cang")) == 1              # 超时才该找玩家商量

    def test_settle_is_idempotent_on_terminal(self):
        """终态不可改写：completed 之后再 settle(failed) 返回 None，状态不变。"""
        led = tl.TaskLedger()
        t = led.book("cang", "goto", desc="去河边")
        led.dispatch_view()
        led.settle(t["task_id"], "completed")

        assert led.settle(t["task_id"], "failed") is None
        assert led._tasks[t["task_id"]]["state"] == "completed"
