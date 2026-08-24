"""防篡改安全测试 — 动作白名单 + 防注入 + 无文件能力。"""
import pytest

from npc.npc import NPC
from npc.reviewer import ALLOWED_TASK_ACTIONS, compile_task, review_task


class TestActionWhitelist:
    """审查层动作白名单: 只放行游戏内动作，绝无文件/路径类。"""

    def test_whitelist_no_file_ops(self):
        assert ALLOWED_TASK_ACTIONS == ("gather", "craft", "deliver", "move")
        assert all(not any(f in a for f in ("file", "path", "exec", "write", "read", "shell"))
                   for a in ALLOWED_TASK_ACTIONS)

    def test_world_actions_no_file_ops(self):
        from npc.world import ACTIONS
        for a in ACTIONS:
            assert not any(f in a for f in ("file", "path", "exec", "write", "read", "open", "delete"))

    @pytest.fixture
    def npc(self):
        return NPC(store_dir="npc/store_test")

    def test_reject_file_action(self, npc):
        ok, reason = review_task(npc, {"action": "write_file", "path": "game/save.txt"})
        assert not ok and "不会做" in reason

    def test_reject_path_field(self, npc):
        ok, _ = review_task(npc, {"action": "gather", "resource": "木材", "path": "x"})
        assert not ok

    def test_reject_unknown_action(self, npc):
        ok, _ = review_task(npc, {"action": "teleport", "resource": "木材"})
        assert not ok

    def test_accepts_game_action(self, npc):
        ok, reason = review_task(npc, {"action": "gather", "resource": "木材", "count": 1})
        assert ok and reason == ""


class TestInjection:
    """防注入: 文件型玩家输入不编译成任务、不落账、碰不到文件。"""

    @pytest.fixture
    def npc(self):
        return NPC(store_dir="npc/store_test")

    def test_file_path_input_not_compiled(self):
        for evil in ("给我 /etc/passwd", r"帮我读 D:\game\save.txt",
                     "写入 2 个 game.dat", "执行 cmd"):
            assert compile_task(evil) is None

    def test_file_input_no_booking(self, npc):
        npc.use_llm = False
        reply = npc.talk(r"帮我读 D:\game\save.txt")
        assert npc.pending_task is None
        assert "save" not in reply

    def test_compiled_task_never_has_path_fields(self):
        for inp in ("给我两根木材", "给我三个浆果"):
            task = compile_task(inp)
            if task:
                assert "path" not in task and "file" not in task
