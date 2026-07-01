"""
文件操作工具 — read_file / write_file / list_dir
"""
import os
from pathlib import Path
from typing import Any

from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus


class ReadFileTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_file",
            description="读取文件内容。支持指定行范围。",
            parameters={
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对路径或相对于工作目录）"},
                    "offset": {"type": "integer", "description": "起始行号（1-indexed），默认 1"},
                    "limit": {"type": "integer", "description": "读取行数，默认 2000"},
                },
                "required": ["path"],
            },
            category="file",
            tags=["file", "read", "io"],
            is_readonly=True,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        path = call.input.get("path", "")
        if not path:
            return False, "参数 'path' 是必需的"
        # 安全检查: 禁止读取敏感路径
        dangerous = ["/etc/passwd", "/etc/shadow", "C:\\Windows\\System32"]
        if any(d in str(path) for d in dangerous):
            return False, f"安全限制: 不允许读取路径 '{path}'"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        path = Path(call.input["path"])
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=f"文件不存在: {path}")
        if path.is_dir():
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=f"路径是目录: {path}")

        offset = call.input.get("offset", 1)
        limit = call.input.get("limit", 2000)

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            total = len(lines)
            selected = lines[offset-1 : offset-1 + limit]
            content = "".join(selected)
            return ToolResult(
                call_id=call.call_id, tool=call.tool,
                status=ToolResultStatus.SUCCESS,
                data={"content": content, "total_lines": total, "read_lines": len(selected), "offset": offset, "path": str(path)},
            )
        except Exception as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=str(e))


class WriteFileTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="write_file",
            description="创建或覆写文件。需要用户批准（除非已授权）。",
            parameters={
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["path", "content"],
            },
            category="file",
            tags=["file", "write", "io"],
            is_readonly=False,
            requires_approval=True,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        path = call.input.get("path", "")
        if not path:
            return False, "参数 'path' 是必需的"
        content = call.input.get("content", "")
        if not content and content != "":
            return False, "参数 'content' 是必需的"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        path = Path(call.input["path"])
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(call.input["content"])
            return ToolResult(
                call_id=call.call_id, tool=call.tool,
                status=ToolResultStatus.SUCCESS,
                data={"path": str(path), "bytes_written": len(call.input["content"])},
            )
        except Exception as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=str(e))


class ListDirTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_dir",
            description="列出目录内容（文件和子目录）。",
            parameters={
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认当前目录"},
                    "pattern": {"type": "string", "description": "可选 glob 过滤，如 '*.py'"},
                },
                "required": [],
            },
            category="file",
            tags=["file", "list", "io"],
            is_readonly=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        path = Path(call.input.get("path", "."))
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=f"目录不存在: {path}")

        try:
            pattern = call.input.get("pattern", "*")
            items = sorted(path.glob(pattern))
            files = [{"name": p.name, "path": str(p), "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0} for p in items]
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"items": files, "count": len(files)})
        except Exception as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=str(e))
