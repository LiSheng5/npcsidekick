"""
工具注册表 — 全局工具发现与管理。

所有工具在此注册后, ToolRouter 才能发现和调用它们。
支持按类别、标签、名称动态查询。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Iterator
from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall


class ToolRegistry:
    """
    工具注册表 — 单例模式, 全局唯一。

    使用方式:
      registry = ToolRegistry()
      registry.register(MyTool())
      tool = registry.get("my_tool")
      tools = registry.find_by_category("file")
    """

    def __init__(self):
        self._tools: Dict[str, ToolProtocol] = {}
        self._schemas: Dict[str, ToolSchema] = {}

    # ── 注册 / 注销 ──────────────────────────────────

    def register(self, tool: ToolProtocol) -> None:
        """注册一个工具。同名工具会被覆盖。"""
        self._tools[tool.name] = tool
        self._schemas[tool.name] = tool.schema

    def register_many(self, tools: List[ToolProtocol]) -> None:
        for t in tools:
            self.register(t)

    def unregister(self, name: str) -> bool:
        """注销工具, 返回是否成功。"""
        removed = self._tools.pop(name, None)
        self._schemas.pop(name, None)
        return removed is not None

    # ── 查询 ─────────────────────────────────────────

    def get(self, name: str) -> Optional[ToolProtocol]:
        """按名称获取工具。"""
        return self._tools.get(name)

    def get_schema(self, name: str) -> Optional[ToolSchema]:
        """按名称获取工具 Schema。"""
        return self._schemas.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_all(self) -> List[ToolProtocol]:
        return list(self._tools.values())

    def list_names(self) -> List[str]:
        return list(self._tools.keys())

    def find_by_category(self, category: str) -> List[ToolProtocol]:
        """按类别查找工具 (file / code / web / system / memory)。"""
        return [t for t in self._tools.values() if t.schema.category == category]

    def find_by_tag(self, tag: str) -> List[ToolProtocol]:
        """按标签查找。"""
        return [t for t in self._tools.values() if tag in t.schema.tags]

    def search(self, query: str) -> List[ToolProtocol]:
        """模糊搜索: 匹配名称、描述、标签。"""
        q = query.lower()
        results = []
        for t in self._tools.values():
            s = t.schema
            if (q in s.name.lower()
                or q in s.description.lower()
                or any(q in tag.lower() for tag in s.tags)):
                results.append(t)
        return results

    # ── LLM-facing schemas ────────────────────────────

    def to_openai_tools(self) -> List[dict]:
        """导出为 OpenAI tools 数组。"""
        return [t.schema.to_openai_function() for t in self._tools.values()]

    def to_tool_descriptions(self) -> str:
        """生成人类/LLM 可读的工具列表。"""
        lines = []
        for t in self._tools.values():
            s = t.schema
            cat = f"[{s.category}]"
            ro = "(readonly)" if s.is_readonly else "(mutate)"
            lines.append(f"- **{s.name}** {cat} {ro}: {s.description}")
        return "\n".join(lines)

    def validate_call(self, name: str) -> Optional[str]:
        """验证工具名是否存在, 返回 None = 存在, str = 错误信息。"""
        if name not in self._tools:
            available = ", ".join(sorted(self._tools.keys()))
            return f"未知工具 '{name}'。可用工具: {available}"
        return None

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[ToolProtocol]:
        return iter(self._tools.values())

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# 全局单例
_global_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry
