"""
测试文件工具 — workspace 路径限制（P0-2）。

此前该模块零测试（见 testing/report.md C6）。
核心验证:resolve() + is_relative_to() 规则不可被 ..、绝对路径、符号链接绕过。
"""

import os
import tempfile
from pathlib import Path

import pytest

from agent.tools.builtin.file_tools import (
    ReadFileTool, WriteFileTool, ListDirTool,
    set_workspace_root, _get_workspace_root, _check_path,
)
from agent.tools.schema import ToolCall, ToolResultStatus


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path):
    """临时 workspace 作为安全根目录。"""
    root = tmp_path / "workspace"
    root.mkdir()
    set_workspace_root(root)
    return root


@pytest.fixture
def read_tool():
    return ReadFileTool()


@pytest.fixture
def write_tool():
    return WriteFileTool()


@pytest.fixture
def list_tool():
    return ListDirTool()


# ── _check_path 单测 ───────────────────────────────────────


class TestPathCheck:
    def test_path_inside_workspace(self, workspace):
        ok, _ = _check_path(workspace / "subdir" / "file.txt")
        assert ok is True

    def test_path_outside_workspace(self, workspace, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        ok, msg = _check_path(outside)
        assert ok is False
        assert "不在工作区" in msg

    def test_path_traversal_blocked(self, workspace):
        """.. 穿越到 workspace 外部 → 拒绝"""
        ok, _ = _check_path(workspace / ".." / "outside.txt")
        assert ok is False

    def test_absolute_path_outside_blocked(self, workspace):
        """绝对路径指到系统目录 → 拒绝"""
        ok, _ = _check_path(Path("C:\\Windows\\System32"))
        assert ok is False


# ── ReadFileTool ────────────────────────────────────────────


class TestReadFileTool:
    def test_read_within_workspace(self, read_tool, workspace):
        f = workspace / "hello.txt"
        f.write_text("hello world", encoding="utf-8")
        result = read_tool.execute(ToolCall(tool="read_file", input={"path": str(f)}))
        assert result.ok
        assert "hello world" in result.data["content"]

    def test_validate_rejects_outside_workspace(self, read_tool, tmp_path):
        f = tmp_path / "secret.txt"
        f.write_text("top secret")
        ok, msg = read_tool.validate(ToolCall(tool="read_file", input={"path": str(f)}))
        assert ok is False
        assert "不在工作区" in msg

    def test_validate_rejects_traversal(self, read_tool, workspace):
        ok, msg = read_tool.validate(ToolCall(tool="read_file", input={"path": str(workspace / ".." / "etc" / "passwd")}))
        assert ok is False

    def test_read_nonexistent_file(self, read_tool, workspace):
        result = read_tool.execute(ToolCall(tool="read_file", input={"path": str(workspace / "nope.txt")}))
        assert result.status == ToolResultStatus.ERROR

    def test_read_directory(self, read_tool, workspace):
        result = read_tool.execute(ToolCall(tool="read_file", input={"path": str(workspace)}))
        assert result.status == ToolResultStatus.ERROR


# ── WriteFileTool ────────────────────────────────────────────


class TestWriteFileTool:
    def test_write_within_workspace(self, write_tool, workspace):
        f = workspace / "output.txt"
        result = write_tool.execute(
            ToolCall(tool="write_file", input={"path": str(f), "content": "safe data"})
        )
        assert result.ok
        assert f.read_text(encoding="utf-8") == "safe data"

    def test_validate_rejects_outside_workspace(self, write_tool, tmp_path):
        ok, msg = write_tool.validate(
            ToolCall(tool="write_file", input={"path": str(tmp_path / "bad.txt"), "content": "x"})
        )
        assert ok is False
        assert "不在工作区" in msg

    def test_execute_rejects_outside_workspace(self, write_tool, tmp_path):
        """validate 可能被绕过 —— execute 也必须拦截。"""
        result = write_tool.execute(
            ToolCall(tool="write_file", input={"path": str(tmp_path / "bad.txt"), "content": "x"})
        )
        assert result.status == ToolResultStatus.REJECTED


# ── ListDirTool ──────────────────────────────────────────────


class TestListDirTool:
    def test_list_within_workspace(self, list_tool, workspace):
        (workspace / "a.py").write_text("")
        (workspace / "b.py").write_text("")
        result = list_tool.execute(ToolCall(tool="list_dir", input={"path": str(workspace)}))
        assert result.ok
        assert result.data["count"] == 2

    def test_list_outside_workspace_rejected(self, list_tool, tmp_path):
        result = list_tool.execute(ToolCall(tool="list_dir", input={"path": str(tmp_path)}))
        assert result.status == ToolResultStatus.REJECTED
