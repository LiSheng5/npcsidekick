"""
代码工具 — 运行代码 / lint 检查
"""
import subprocess
import tempfile
import os
from pathlib import Path

from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus


class RunCodeTool(ToolProtocol):
    """
    在隔离的临时目录中运行代码片段。
    支持 Python 代码执行。
    """
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="run_code",
            description="在隔离环境中运行 Python 代码片段并返回输出。",
            parameters={
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码"},
                    "language": {"type": "string", "description": "编程语言，默认 'python'"},
                    "timeout_sec": {"type": "integer", "description": "超时秒数，默认 30"},
                },
                "required": ["code"],
            },
            category="code",
            tags=["code", "run", "execute", "python"],
            is_readonly=False,
            requires_approval=True,
            estimated_duration_ms=5000,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        code = call.input.get("code", "")
        if not code:
            return False, "参数 'code' 是必需的"
        # 禁止危险操作
        dangerous = ["__import__", "subprocess", "os.system", "shutil.rmtree",
                     "eval(", "exec(", "compile(", "open(", "write(", "delete"]
        for kw in dangerous:
            if kw in code:
                return False, f"安全限制: 代码包含潜在危险操作 '{kw}'"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        code = call.input["code"]
        timeout = call.input.get("timeout_sec", 30)

        try:
            result = subprocess.run(
                ["python", "-c", code],
                capture_output=True, text=True,
                timeout=timeout,
                cwd=str(Path.cwd()),
            )
            return ToolResult(
                call_id=call.call_id, tool=call.tool,
                status=ToolResultStatus.SUCCESS,
                data={
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.TIMEOUT, error=f"代码执行超时 ({timeout}s)")
        except Exception as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=str(e))


class LintCodeTool(ToolProtocol):
    """对 Python 代码执行静态检查 (flake8 / pyflakes 如果可用)。"""
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="lint_code",
            description="对 Python 代码执行静态检查，返回风格/错误问题。",
            parameters={
                "properties": {
                    "code": {"type": "string", "description": "要检查的 Python 代码"},
                    "filename": {"type": "string", "description": "代码的文件名（用于错误报告），默认 '<string>'"},
                },
                "required": ["code"],
            },
            category="code",
            tags=["code", "lint", "check", "python"],
            is_readonly=True,
            estimated_duration_ms=3000,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        code = call.input["code"]
        fname = call.input.get("filename", "<string>")

        # 尝试使用 Python 内置 compile() 做基础语法检查
        try:
            compile(code, fname, "exec")
            syntax_ok = True
            syntax_error = None
        except SyntaxError as e:
            syntax_ok = False
            syntax_error = f"第 {e.lineno} 行: {e.msg}"

        # 尝试 pyflakes (如果可用)
        pyflakes_issues = []
        try:
            result = subprocess.run(
                ["python", "-m", "pyflakes", "-"],
                input=code, capture_output=True, text=True, timeout=10,
            )
            if result.stdout:
                pyflakes_issues = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # pyflakes 不可用

        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS,
            data={
                "syntax_ok": syntax_ok,
                "syntax_error": syntax_error,
                "lint_issues": pyflakes_issues,
                "issue_count": len(pyflakes_issues) + (0 if syntax_ok else 1),
            },
        )
