"""
Tests for agent.tools.builtin.code_tools — _scan_code_ast, RunCodeTool, LintCodeTool.
"""
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from agent.tools.schema import ToolCall, ToolResultStatus
from agent.tools.builtin.code_tools import (
    _scan_code_ast,
    _get_func_name,
    RunCodeTool,
    LintCodeTool,
    FORBIDDEN_MODULES,
)


# ═══════════════════════════════════════════════════════
# _scan_code_ast
# ═══════════════════════════════════════════════════════

class TestScanCodeAST:
    def test_valid_code_no_issues(self):
        issues = _scan_code_ast("print('hello')\nx = 1 + 2")
        assert issues == []

    def test_syntax_error(self):
        issues = _scan_code_ast("def foo(:")
        assert len(issues) == 1
        assert "语法错误" in issues[0]

    def test_forbidden_import_os(self):
        issues = _scan_code_ast("import os")
        assert len(issues) == 1
        assert "os" in issues[0]
        assert "禁止 import" in issues[0]

    def test_forbidden_import_from(self):
        issues = _scan_code_ast("from subprocess import run")
        assert len(issues) == 1
        assert "subprocess" in issues[0]

    def test_forbidden_import_nested(self):
        """os.path.split('.') -> base module is 'os' which is forbidden."""
        issues = _scan_code_ast("import os.path")
        assert len(issues) == 1
        assert "os.path" in issues[0]

    def test_safe_import_not_flagged(self):
        """Importing a module not in FORBIDDEN_MODULES should pass."""
        issues = _scan_code_ast("import math\nfrom collections import defaultdict")
        assert issues == []

    def test_eval_blocked(self):
        issues = _scan_code_ast('eval("1+1")')
        assert len(issues) == 1
        assert "eval" in issues[0]

    def test_exec_blocked(self):
        issues = _scan_code_ast('exec("print(1)")')
        assert len(issues) == 1
        assert "exec" in issues[0]

    def test_compile_blocked(self):
        issues = _scan_code_ast('compile("x=1", "", "exec")')
        assert len(issues) == 1
        assert "compile" in issues[0]

    def test_open_blocked(self):
        issues = _scan_code_ast("open('/etc/passwd')")
        assert len(issues) == 1
        assert "open" in issues[0]

    def test_dunder_subclasses_blocked(self):
        issues = _scan_code_ast('obj.__subclasses__()')
        assert len(issues) == 1
        assert "__subclasses__" in issues[0]

    def test_dunder_bases_blocked(self):
        issues = _scan_code_ast('obj.__bases__')
        assert len(issues) == 1
        assert "__bases__" in issues[0]

    def test_dunder_globals_blocked(self):
        issues = _scan_code_ast('obj.__globals__')
        assert len(issues) == 1
        assert "__globals__" in issues[0]

    def test_safe_dunder_not_flagged(self):
        """Only dangerous dunders are flagged, not all dunders."""
        issues = _scan_code_ast('obj.__custom__')
        # __custom__ is not in the dangerous set
        assert issues == []

    def test_print_not_flagged(self):
        issues = _scan_code_ast('print("allowed")')
        assert issues == []

    def test_empty_code(self):
        issues = _scan_code_ast("")
        assert issues == []

    def test_multiple_issues(self):
        issues = _scan_code_ast("import os\nimport subprocess\neval('1')")
        assert len(issues) == 3

    def test_comments_ignored(self):
        """AST scanning ignores comments—dangerous text in comments is not flagged."""
        issues = _scan_code_ast("# import os and eval() are dangerous\nx = 1")
        assert issues == []


# ═══════════════════════════════════════════════════════
# RunCodeTool
# ═══════════════════════════════════════════════════════

class TestRunCodeTool:
    def test_validate_empty_code(self):
        tool = RunCodeTool()
        ok, msg = tool.validate(ToolCall(tool="run_code", input={"code": ""}))
        assert ok is False
        assert "不能为空" in msg

    def test_validate_whitespace_only(self):
        tool = RunCodeTool()
        ok, msg = tool.validate(ToolCall(tool="run_code", input={"code": "   "}))
        assert ok is False
        assert "不能为空" in msg

    def test_validate_too_long(self):
        tool = RunCodeTool()
        ok, msg = tool.validate(ToolCall(tool="run_code", input={"code": "x" * 100_001}))
        assert ok is False
        assert "过长" in msg

    def test_validate_dangerous_code(self):
        tool = RunCodeTool()
        ok, msg = tool.validate(ToolCall(tool="run_code", input={"code": "import os"}))
        assert ok is False
        assert "安全检查失败" in msg

    def test_validate_safe_code(self):
        tool = RunCodeTool()
        ok, msg = tool.validate(ToolCall(tool="run_code", input={"code": "print('hello')\nx = sum([1,2,3])"}))
        assert ok is True

    def test_validate_missing_code_key(self):
        tool = RunCodeTool()
        ok, msg = tool.validate(ToolCall(tool="run_code", input={}))
        assert ok is False

    def test_execute_success(self):
        tool = RunCodeTool()
        mock_result = MagicMock()
        mock_result.stdout = "hello world\n"
        mock_result.stderr = ""
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result), \
             patch("tempfile.mkdtemp", return_value="/tmp/dagent_test_sandbox"):
            result = tool.execute(ToolCall(tool="run_code", input={"code": "print('hello world')"}))

        assert result.status == ToolResultStatus.SUCCESS
        assert "hello world" in result.data["stdout"]
        assert result.data["returncode"] == 0

    def test_execute_with_stderr(self):
        tool = RunCodeTool()
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "Warning: something\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result), \
             patch("tempfile.mkdtemp", return_value="/tmp/dagent_test_sandbox"):
            result = tool.execute(ToolCall(tool="run_code", input={"code": "import sys; print('ok', file=sys.stderr)"}))

        assert result.status == ToolResultStatus.SUCCESS
        assert "Warning" in result.data["stderr"]

    def test_execute_nonzero_returncode(self):
        tool = RunCodeTool()
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "NameError: name 'x' is not defined"
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result), \
             patch("tempfile.mkdtemp", return_value="/tmp/dagent_test_sandbox"):
            result = tool.execute(ToolCall(tool="run_code", input={"code": "print(x)"}))

        # Non-zero return code still returns SUCCESS—the code ran, just errored
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["returncode"] == 1

    def test_execute_timeout(self):
        tool = RunCodeTool()

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="python", timeout=5)), \
             patch("tempfile.mkdtemp", return_value="/tmp/dagent_test_sandbox"):
            result = tool.execute(ToolCall(tool="run_code", input={"code": "while True: pass"}))

        assert result.status == ToolResultStatus.TIMEOUT
        assert "超时" in result.error

    def test_execute_timeout_sec_default(self):
        tool = RunCodeTool()

        with patch("subprocess.run", return_value=MagicMock(stdout="ok", stderr="", returncode=0)) as mock_run, \
             patch("tempfile.mkdtemp", return_value="/tmp/sandbox"):
            tool.execute(ToolCall(tool="run_code", input={"code": "print(1)"}))

        # timeout should default to 10 when not provided
        assert mock_run.call_args[1]["timeout"] == 10

    def test_execute_timeout_sec_explicit(self):
        tool = RunCodeTool()

        with patch("subprocess.run", return_value=MagicMock(stdout="ok", stderr="", returncode=0)) as mock_run, \
             patch("tempfile.mkdtemp", return_value="/tmp/sandbox"):
            tool.execute(ToolCall(tool="run_code", input={"code": "print(1)", "timeout_sec": 5}))

        assert mock_run.call_args[1]["timeout"] == 5

    def test_execute_timeout_sec_clamped(self):
        tool = RunCodeTool()

        with patch("subprocess.run", return_value=MagicMock(stdout="ok", stderr="", returncode=0)) as mock_run, \
             patch("tempfile.mkdtemp", return_value="/tmp/sandbox"):
            tool.execute(ToolCall(tool="run_code", input={"code": "print(1)", "timeout_sec": 999}))

        # Clamped to 30
        assert mock_run.call_args[1]["timeout"] == 30

    def test_execute_generic_exception(self):
        tool = RunCodeTool()

        with patch("subprocess.run", side_effect=RuntimeError("sandbox crash")), \
             patch("tempfile.mkdtemp", return_value="/tmp/sandbox"):
            result = tool.execute(ToolCall(tool="run_code", input={"code": "print(1)"}))

        assert result.status == ToolResultStatus.ERROR
        assert "RuntimeError" in result.error


# ═══════════════════════════════════════════════════════
# LintCodeTool
# ═══════════════════════════════════════════════════════

class TestLintCodeTool:
    def test_validate_empty_code(self):
        tool = LintCodeTool()
        ok, msg = tool.validate(ToolCall(tool="lint_code", input={"code": ""}))
        assert ok is False
        assert "必需" in msg

    def test_validate_non_empty_code(self):
        tool = LintCodeTool()
        ok, msg = tool.validate(ToolCall(tool="lint_code", input={"code": "x = 1"}))
        assert ok is True

    def test_execute_valid_code(self):
        tool = LintCodeTool()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            result = tool.execute(ToolCall(tool="lint_code", input={"code": "x = 1"}))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["syntax_ok"] is True
        assert result.data["syntax_error"] is None
        assert result.data["total_issues"] == 0

    def test_execute_syntax_error(self):
        tool = LintCodeTool()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            result = tool.execute(ToolCall(tool="lint_code", input={"code": "def foo(:", "filename": "test.py"}))

        assert result.data["syntax_ok"] is False
        assert result.data["syntax_error"] is not None
        assert result.data["total_issues"] >= 1

    def test_execute_security_issue(self):
        tool = LintCodeTool()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            result = tool.execute(ToolCall(tool="lint_code", input={"code": "import os; eval('1')"}))

        assert result.data["syntax_ok"] is True
        assert len(result.data["security_issues"]) >= 1
        assert result.data["total_issues"] >= 1

    def test_execute_pyflakes_unavailable(self):
        tool = LintCodeTool()

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = tool.execute(ToolCall(tool="lint_code", input={"code": "x = 1"}))

        # Should gracefully handle missing pyflakes
        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["syntax_ok"] is True
        assert result.data["lint_issues"] == []

    def test_execute_pyflakes_timeout(self):
        tool = LintCodeTool()

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pyflakes", timeout=10)):
            result = tool.execute(ToolCall(tool="lint_code", input={"code": "x = 1"}))

        # Should gracefully handle pyflakes timeout
        assert result.status == ToolResultStatus.SUCCESS

    def test_execute_default_filename(self):
        tool = LintCodeTool()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="")
            result = tool.execute(ToolCall(tool="lint_code", input={"code": "x=1"}))

        assert result.status == ToolResultStatus.SUCCESS
