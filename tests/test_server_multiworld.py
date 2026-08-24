"""多世界隔离 + 推送通道（2026-08-23）— world_id 守卫纯函数与 SSE 路由。"""
import pytest

from npc.server import world_mismatch


def test_world_mismatch_semantics():
    """守卫语义: 双方都声明且不一致才拦; 单世界/旧客户端零回归。"""
    assert world_mismatch("gta", "godot") is True      # 接错线 → 拦
    assert world_mismatch("gta", "gta") is False       # 匹配 → 放行
    assert world_mismatch("", "anything") is False     # 实例未配 world_id → 零回归
    assert world_mismatch("gta", None) is False        # 旧客户端不带 world_id → 放行
    assert world_mismatch("gta", "") is False


def test_manifest_resources_stash():
    """清单 resources 段暂存/清空(server 启动时与世界合并)。"""
    from npc.reviewer import get_manifest_resources, set_manifest_resources
    set_manifest_resources({"羽毛": ["毛"]})
    assert get_manifest_resources() == {"羽毛": ["毛"]}
    set_manifest_resources(None)
    assert get_manifest_resources() is None
