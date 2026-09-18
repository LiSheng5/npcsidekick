"""
NPCSidekick — 游戏行动工具（game_tools）。

把世界契约的行动集包装成 NPCSidekick ToolProtocol 工具，
NPC 通过 ToolRouter 统一调度 — 用户扩展世界时在接缝上注册自己的工具。
"""
from __future__ import annotations

from typing import Dict

from agent.tools.schema import (
    ToolProtocol,
    ToolSchema,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from npc.world import observe, apply_action


class LookTool(ToolProtocol):
    """感知: 查看当前所在位置和资源。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="look",
            description="查看你当前位置的环境: 资源、可前往的地点、主角是否在附近。",
            parameters={"type": "object", "properties": {}, "required": []},
            category="npc_world",
            tags=["perception", "world"],
            is_readonly=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS,
            data={"observation": observe(self.world, who=self.actor_id)},
        )


class CheckInventoryTool(ToolProtocol):
    """感知: 查看背包。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="check_inventory",
            description="查看你的背包里有什么。",
            parameters={"type": "object", "properties": {}, "required": []},
            category="npc_world",
            tags=["perception", "inventory"],
            is_readonly=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS,
            data={"inventory": dict(self.world["actors"][self.actor_id]["inventory"])},
        )


class MoveTool(ToolProtocol):
    """行动: 移动到可到达的地点。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="move",
            description="移动到另一个地点（只能去可到达的地方）。",
            parameters={
                "properties": {
                    "dest": {"type": "string", "description": "目的地名称（用世界里的地点名）"},
                },
                "required": ["dest"],
            },
            category="npc_world",
            tags=["action", "movement"],
            is_readonly=False,
            is_idempotent=True,
            estimated_duration_ms=200,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        if not call.input.get("dest"):
            return False, "参数 'dest' 是必需的"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        self.world, ok, message = apply_action(self.world, "move", call.input, who=self.actor_id)
        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS if ok else ToolResultStatus.ERROR,
            data={"message": message},
            error=None if ok else message,
        )


class GatherTool(ToolProtocol):
    """行动: 采集当前地点的资源。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="gather",
            description="采集当前地点的资源到背包（每次 1 个）。",
            parameters={
                "properties": {
                    "resource": {"type": "string", "description": "要采集的资源名（用世界里的资源名）"},
                },
                "required": ["resource"],
            },
            category="npc_world",
            tags=["action", "resource"],
            is_readonly=False,
            is_idempotent=False,
            estimated_duration_ms=500,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        if not call.input.get("resource"):
            return False, "参数 'resource' 是必需的"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        self.world, ok, message = apply_action(self.world, "gather", call.input, who=self.actor_id)
        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS if ok else ToolResultStatus.ERROR,
            data={"message": message},
            error=None if ok else message,
        )


class DeliverTool(ToolProtocol):
    """行动: 把背包物资交付给主角。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="deliver",
            description="把背包里的物资交付给主角（主角必须在你身边）。",
            parameters={
                "properties": {
                    "resource": {"type": "string", "description": "要交付的资源名（用世界里的资源名）"},
                },
                "required": ["resource"],
            },
            category="npc_world",
            tags=["action", "delivery"],
            is_readonly=False,
            is_idempotent=False,
            estimated_duration_ms=300,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        if not call.input.get("resource"):
            return False, "参数 'resource' 是必需的"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        self.world, ok, message = apply_action(self.world, "deliver", call.input, who=self.actor_id)
        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS if ok else ToolResultStatus.ERROR,
            data={"message": message},
            error=None if ok else message,
        )


class SayTool(ToolProtocol):
    """行动: 说话（记录到世界日志）。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="say",
            description="说一句话（会记录在世界日志里）。",
            parameters={
                "properties": {
                    "text": {"type": "string", "description": "要说的话"},
                },
                "required": ["text"],
            },
            category="npc_world",
            tags=["action", "dialogue"],
            is_readonly=False,
            is_idempotent=True,
            estimated_duration_ms=100,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        if not call.input.get("text"):
            return False, "参数 'text' 是必需的"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        self.world, ok, message = apply_action(self.world, "say", call.input, who=self.actor_id)
        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS if ok else ToolResultStatus.ERROR,
            data={"message": message},
        )


def register_world_tools(registry, world: Dict, actor_id: str = "cang") -> None:
    """把全部世界工具注册进 ToolRegistry，并注入共享世界状态 + 角色绑定。"""
    tools = [LookTool, CheckInventoryTool, MoveTool, GatherTool, DeliverTool, SayTool]
    for tool_cls in tools:
        tool = tool_cls()
        tool.world = world       # 共享世界状态引用（世界契约的注入点）
        tool.actor_id = actor_id  # 此工具组服务哪个 NPC（多 NPC 时各绑各的）
        registry.register(tool)
