"""game_tools 测试 — 世界工具遵循 ToolProtocol 契约。"""
import pytest

from agent.tools.registry import ToolRegistry
from agent.tools.router import ToolRouter
from agent.tools.schema import ToolCall, ToolResultStatus
from npc.game_tools import register_world_tools
from npc.world import default_world


@pytest.fixture
def router_and_world():
    world = default_world()
    registry = ToolRegistry()
    register_world_tools(registry, world)
    router = ToolRouter(registry=registry)
    return router, world


class TestToolRegistration:
    def test_all_world_tools_registered(self, router_and_world):
        router, _ = router_and_world
        names = {t.name for t in router.registry.list_all()}
        assert {"look", "check_inventory", "move", "gather", "deliver", "say"} <= names


class TestMoveTool:
    def test_move_success(self, router_and_world):
        router, world = router_and_world
        result = router.dispatch(ToolCall(tool="move", input={"dest": "森林"}))
        assert result.status == ToolResultStatus.SUCCESS
        assert world["actors"]["cang"]["position"] == "森林"

    def test_move_missing_param_rejected(self, router_and_world):
        router, _ = router_and_world
        result = router.dispatch(ToolCall(tool="move", input={}))
        assert result.status == ToolResultStatus.REJECTED


class TestGatherTool:
    def test_gather_flow(self, router_and_world):
        router, world = router_and_world
        router.dispatch(ToolCall(tool="move", input={"dest": "森林"}))
        result = router.dispatch(ToolCall(tool="gather", input={"resource": "木材"}))
        assert result.status == ToolResultStatus.SUCCESS
        assert world["actors"]["cang"]["inventory"]["木材"] == 1


class TestLookTool:
    def test_look_is_readonly(self, router_and_world):
        router, world = router_and_world
        before = dict(world)
        result = router.dispatch(ToolCall(tool="look", input={}))
        assert result.status == ToolResultStatus.SUCCESS
        assert "observation" in result.data
        assert world == before  # 感知不改世界


class TestDeliverTool:
    def test_deliver_end_to_end(self, router_and_world):
        router, world = router_and_world
        router.dispatch(ToolCall(tool="move", input={"dest": "森林"}))
        router.dispatch(ToolCall(tool="gather", input={"resource": "木材"}))
        router.dispatch(ToolCall(tool="move", input={"dest": "村庄"}))
        result = router.dispatch(ToolCall(tool="deliver", input={"resource": "木材"}))
        assert result.status == ToolResultStatus.SUCCESS
        assert world["delivered"]["木材"] == 1
