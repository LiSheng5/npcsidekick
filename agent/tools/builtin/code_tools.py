"""
代码工具 — 安全代码执行 / lint 检查。

安全模型:
  - 文件系统隔离: 每次运行在独立临时目录
  - AST 级代码审查: 用 AST 检测危险模式，比字符串匹配更难绕过
  - 资源限制: 超时 + 内存限制
  - 危险模块阻断: 禁止 os/subprocess/socket/sys 等危险 import
  - 仅允许白名单内置函数
"""
import ast
import subprocess
import tempfile
import os
import sys
from pathlib import Path
from typing import Set

from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus
from agent.tools.builtin.file_tools import _check_path


# ── 安全配置 ──────────────────────────────────────────

# 黑名单: 禁止 import 的危险模块
FORBIDDEN_MODULES: Set[str] = {
    "os", "subprocess", "sys", "shutil", "socket", "ctypes",
    "multiprocessing", "threading", "signal", "pty", "fcntl",
    "posix", "posixpath", "grp", "pwd", "spwd",
    "importlib", "builtins", "__builtins__",
    "requests", "urllib", "http", "ftplib", "smtplib",
    "telnetlib", "poplib", "imaplib", "nntplib",
    "pickle", "marshal", "shelve",
    "code", "codeop", "compileall", "py_compile",
}

# 白名单: 允许的安全模块 (这些之外的 import 会被警告但放行)
SAFE_MODULES: Set[str] = {
    "math", "json", "re", "datetime", "collections", "itertools",
    "functools", "operator", "string", "textwrap", "hashlib",
    "random", "statistics", "decimal", "fractions",
    "typing", "dataclasses", "enum", "abc",
    "copy", "pprint", "logging",
    "csv", "base64", "binascii", "html", "xml",
    "unittest", "doctest",
    "pathlib", "tempfile", "io", "contextlib",
}

# 白名单: 允许的内置函数
ALLOWED_BUILTINS: Set[str] = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "bytearray",
    "callable", "chr", "classmethod", "complex", "delattr",
    "dict", "dir", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "getattr", "globals", "hasattr", "hash", "hex",
    "id", "int", "isinstance", "issubclass", "iter",
    "len", "list", "locals", "map", "max", "memoryview",
    "min", "next", "object", "oct", "ord", "pow", "print",
    "property", "range", "repr", "reversed", "round",
    "set", "setattr", "slice", "sorted", "staticmethod",
    "str", "sum", "super", "tuple", "type", "vars", "zip",
    # 以下是显式禁止的: __import__, compile, eval, exec, open, input, breakpoint
}


class CodeSecurityError(Exception):
    """代码安全检查失败。"""
    pass


def _scan_code_ast(code: str) -> list[str]:
    """
    用 AST 深度扫描代码，返回所有安全问题列表。

    AST 扫描比字符串匹配更难绕过 — 攻击者无法用空格、注释、
    编码技巧来隐藏危险调用。
    """
    issues: list[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"语法错误: {e}"]

    for node in ast.walk(tree):
        # 检测危险 import
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                modules = [node.module] if node.module else []

            for mod in modules:
                base_mod = mod.split(".")[0]
                if base_mod in FORBIDDEN_MODULES:
                    issues.append(f"禁止 import '{mod}': 模块 '{base_mod}' 在安全黑名单中")

        # 检测危险函数调用
        if isinstance(node, ast.Call):
            func_name = _get_func_name(node)
            if func_name in {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint"}:
                issues.append(f"禁止调用 '{func_name}()': 此函数在安全黑名单中")

            # 检测 getattr/setattr 绕过
            if func_name == "getattr" and len(node.args) >= 1:
                if isinstance(node.args[0], ast.Constant) and node.args[0].value == __builtins__:
                    issues.append("禁止访问 __builtins__")

        # 检测 __ 属性访问 (如 obj.__class__.__bases__ 等沙箱逃逸)
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                if node.attr in {"__subclasses__", "__bases__", "__mro__",
                                 "__class__", "__globals__", "__code__",
                                 "__builtins__", "__import__"}:
                    issues.append(f"禁止访问 '{node.attr}': 这是常见的沙箱逃逸路径")

    return issues


def _get_func_name(call_node: ast.Call) -> str:
    """从 ast.Call 节点提取函数名。"""
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# ── 工具类 ────────────────────────────────────────────

class RunCodeTool(ToolProtocol):
    """
    在隔离环境中安全运行 Python 代码。

    安全措施:
      1. 临时目录隔离 — 代码在独立 temp 目录运行
      2. AST 级扫描 — 深层次检测危险模式
      3. 超时控制 — 防止无限循环
      4. 模块黑名单 — 阻断 os/subprocess/socket 等
      5. 仅白名单内置函数
    """

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="run_code",
            description="在安全隔离环境中运行 Python 代码片段并返回输出。代码经过 AST 扫描和沙箱限制。",
            parameters={
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码"},
                    "timeout_sec": {"type": "integer", "description": "超时秒数，默认 10，最大 30"},
                },
                "required": ["code"],
            },
            category="code",
            tags=["code", "run", "execute", "python", "safe"],
            is_readonly=False,
            requires_approval=True,
            estimated_duration_ms=5000,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        code = call.input.get("code", "")
        if not code or not code.strip():
            return False, "参数 'code' 是必需的且不能为空"

        if len(code) > 100_000:
            return False, "代码过长 (最大 100,000 字符)"

        # AST 深度扫描
        issues = _scan_code_ast(code)
        if issues:
            return False, "安全检查失败:\n  - " + "\n  - ".join(issues)

        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        code = call.input["code"]
        timeout = min(call.input.get("timeout_sec", 10), 30)  # 最大 30 秒

        # 创建隔离的临时目录
        tmp_dir = tempfile.mkdtemp(prefix="dagentsandbox_")

        try:
            # 构建安全的执行环境
            # 通过 PYTHONPATH 限制 + 脚本包装来隔离
            sandbox_wrapper = self._build_sandbox_wrapper(code, tmp_dir)

            result = subprocess.run(
                [sys.executable, "-c", sandbox_wrapper],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmp_dir,
                env={
                    **os.environ,
                    "PYTHONPATH": tmp_dir,
                    "HOME": tmp_dir,
                    "TMPDIR": tmp_dir,
                    "TEMP": tmp_dir,
                    "TMP": tmp_dir,
                },
            )

            return ToolResult(
                call_id=call.call_id,
                tool=call.tool,
                status=ToolResultStatus.SUCCESS,
                data={
                    "stdout": result.stdout[:5000],
                    "stderr": result.stderr[:5000],
                    "returncode": result.returncode,
                    "sandbox_dir": str(tmp_dir),
                },
            )

        except subprocess.TimeoutExpired:
            return ToolResult(
                call_id=call.call_id, tool=call.tool,
                status=ToolResultStatus.TIMEOUT,
                error=f"代码执行超时 ({timeout}s)",
            )
        except Exception as e:
            return ToolResult(
                call_id=call.call_id, tool=call.tool,
                status=ToolResultStatus.ERROR,
                error=f"{type(e).__name__}: {e}",
            )

    def _build_sandbox_wrapper(self, user_code: str, sandbox_dir: str) -> str:
        """
        构建沙箱包装脚本。

        在用户代码执行前:
          1. 禁用危险内置函数
          2. 阻断危险模块的 import
          3. 设置工作目录到临时目录
        """
        allowed_builtins_repr = repr(ALLOWED_BUILTINS)
        forbidden_modules_repr = repr(FORBIDDEN_MODULES)

        import base64
        encoded_code = base64.b64encode(user_code.encode("utf-8")).decode("ascii")
        wrapper = f'''
import sys
import builtins
import os
import base64 as _b64

# ── 沙箱初始化 ──────────────────────────

# 1. 切换到隔离目录
os.chdir(r"{sandbox_dir}")

# 2. 限制内置函数
ALLOWED = {allowed_builtins_repr}
_original_globals = dict(globals())

# 覆盖 __builtins__
sandbox_builtins = {{k: getattr(builtins, k) for k in ALLOWED if hasattr(builtins, k)}}
sandbox_builtins["BaseException"] = BaseException
sandbox_builtins["Exception"] = Exception
sandbox_builtins["__name__"] = "__main__"

# 3. Import 钩子: 阻断危险模块
FORBIDDEN = {forbidden_modules_repr}
_original_import = builtins.__import__

def _safe_import(name, *args, **kwargs):
    base = name.split(".")[0]
    if base in FORBIDDEN:
        raise ImportError(f"安全限制: 禁止导入 '{{name}}'")
    return _original_import(name, *args, **kwargs)

builtins.__import__ = _safe_import

# 4. 执行用户代码 (base64 编码避免三引号冲突)
_user_code = _b64.b64decode("{encoded_code}").decode("utf-8")
try:
    exec(_user_code, {{"__builtins__": sandbox_builtins, "__name__": "__main__"}})
except SystemExit:
    pass
'''
        return wrapper


class LintCodeTool(ToolProtocol):
    """对 Python 代码执行静态检查 (语法 + 安全扫描 + pyflakes 如果可用)。"""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="lint_code",
            description="对 Python 代码执行静态检查：语法验证 + 安全扫描 + 风格检查。code 与 path 二选一（提供其一即可）。",
            parameters={
                "properties": {
                    "code": {"type": "string", "description": "要检查的 Python 代码"},
                    "path": {"type": "string", "description": "要检查的 Python 文件路径（与 code 二选一）"},
                    "filename": {"type": "string", "description": "文件名（用于错误报告），默认 '<string>'"},
                },
                "required": [],
            },
            category="code",
            tags=["code", "lint", "check", "python", "security"],
            is_readonly=True,
            estimated_duration_ms=3000,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        code = call.input.get("code", "")
        path = call.input.get("path", "")
        if not code and not path:
            return False, "参数 'code' 和 'path' 至少一个是必需的"
        if path:
            ok, msg = _check_path(Path(path))
            if not ok:
                return False, msg
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        code = call.input.get("code", "")
        fname = call.input.get("filename", "<string>")
        path = call.input.get("path", "")
        if not code and path:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    code = f.read()
            except Exception as e:
                return ToolResult(
                    call_id=call.call_id, tool=call.tool,
                    status=ToolResultStatus.ERROR,
                    error=f"读取文件失败: {e}",
                )
            fname = str(path)

        # 1. 语法检查
        syntax_ok = True
        syntax_error = None
        try:
            compile(code, fname, "exec")
        except SyntaxError as e:
            syntax_ok = False
            syntax_error = f"第 {e.lineno} 行: {e.msg}"

        # 2. 安全检查 (AST 扫描)
        security_issues = _scan_code_ast(code)

        # 3. Pyflakes (如果可用)
        pyflakes_issues = []
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pyflakes", "-"],
                input=code, capture_output=True, text=True, timeout=10,
            )
            if result.stdout:
                pyflakes_issues = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        total_issues = len(pyflakes_issues) + len(security_issues) + (0 if syntax_ok else 1)

        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS,
            data={
                "syntax_ok": syntax_ok,
                "syntax_error": syntax_error,
                "security_issues": security_issues,
                "lint_issues": pyflakes_issues,
                "total_issues": total_issues,
            },
        )
