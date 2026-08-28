"""日志分档测试（2026-08-28 读侧版, 用户 D1 保守裁决: 归档留全量）。

覆盖: log_tier 五种写入格式判定 + 未知行保守 + summarize_autonomous
计数摘要 + _behavior_log 四场景（互动带摘要/全互动/全自主/空）。
"""
import pytest

from npc.npc import NPC
from npc.world import (LOG_TIER_AUTONOMOUS, LOG_TIER_INTERACTIVE,
                       log_tier, summarize_autonomous)


class TestLogTier:
    def test_five_formats(self):
        assert log_tier("cang 前往 森林") == LOG_TIER_AUTONOMOUS
        assert log_tier("cang 采集了 1 个木材") == LOG_TIER_AUTONOMOUS
        assert log_tier("cang 制作了 木石工具") == LOG_TIER_AUTONOMOUS
        assert log_tier("cang 休息了 3 个 tick") == LOG_TIER_AUTONOMOUS
        assert log_tier("cang 将 木材 交给了 主角") == LOG_TIER_INTERACTIVE
        assert log_tier("cang 说: 这柴火不错") == LOG_TIER_INTERACTIVE

    def test_unknown_conservative(self):
        """未知行保守归 interactive（少记比漏记好）。"""
        assert log_tier("cang 鬼鬼祟祟完成了一件怪事") == LOG_TIER_INTERACTIVE


class TestSummarizeAutonomous:
    def test_counts_by_action(self):
        lines = ["cang 前往 森林", "cang 前往 森林",
                 "cang 采集了 1 个木材", "cang 采集了 1 个木材",
                 "cang 采集了 1 个木材", "cang 制作了 木石工具"]
        out = summarize_autonomous(lines)
        assert "采集木材×3" in out
        assert "前往森林×2" in out
        assert "制作木石工具×1" in out
        assert " · " in out

    def test_rest_and_empty(self):
        assert summarize_autonomous(["cang 休息了 3 个 tick"]) == "休息×1"
        assert summarize_autonomous([]) == ""
        assert summarize_autonomous(["cang 说: 你好"]) == ""   # 互动行不算

    def test_different_resources_separate(self):
        lines = ["cang 采集了 1 个木材", "cang 采集了 1 个浆果"]
        out = summarize_autonomous(lines)
        assert "采集木材×1" in out and "采集浆果×1" in out


@pytest.fixture
def npc(tmp_path):
    return NPC(store_dir=str(tmp_path))


def _log_of(npc, entries):
    npc.world["log"] = [f"{npc.actor_id} " + e for e in entries]
    return npc


class TestBehaviorLogInjection:
    """_behavior_log: 🌟逐条至多 6 + 🌗 摘要一行（问"刚才在干嘛"有整）。"""

    def test_mixed_tier_with_summary(self, npc):
        _log_of(npc, [
            "前往 森林", "采集了 1 个木材", "采集了 1 个木材",
            "将 木材 交给了 主角", "说: 明天再进林子",
        ])
        out = npc._behavior_log()
        assert "将 木材 交给了 主角" in out       # 🌟 逐条
        assert "说: 明天再进林子" in out
        assert "（自主活动）" in out               # 🌗 摘要
        assert "采集木材×2" in out
        assert "前往森林×1" in out

    def test_interactive_only_no_summary(self, npc):
        _log_of(npc, ["说: 你好", "说: 吃了吗"])
        out = npc._behavior_log()
        assert "你好" in out and "吃了吗" in out
        assert "自主活动" not in out               # 无自主行 → 无摘要

    def test_autonomous_only_summary_preserved(self, npc):
        """全自主场景: 只有摘要一行也能回答"刚才在干嘛"。"""
        _log_of(npc, ["前往 森林", "采集了 1 个木材", "休息了 3 个 tick"])
        out = npc._behavior_log()
        assert out.startswith("- （自主活动）")
        assert "采集木材×1" in out and "休息×1" in out
        assert "前往森林×1" in out

    def test_early_stop_window_with_distant_interaction(self, npc):
        """互动行在远处(600 条自主流水后), 早停/窗口仍拿到它。"""
        _log_of(npc, [f"采集了 1 个木材{i:04d}" for i in range(600)]
                + ["说: 刚才我干了些啥"] + ["休息了 3 个 tick"] * 5)
        out = npc._behavior_log()
        assert "刚才我干了些啥" in out
        assert "（自主活动）" in out

    def test_empty_log(self, npc):
        _log_of(npc, [])
        assert npc._behavior_log() == "（最近没干什么）"
