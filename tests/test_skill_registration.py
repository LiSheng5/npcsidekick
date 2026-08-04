"""
测试 AnalyzeCodeSkill 的生产接线 — 真实 ToolRouter + 真实注册表。

审计结论 (见 .claude/checkpoints/agent_2_output.json):
  1. AnalyzeCodeSkill 从未在 AgentOrchestrator._register_builtin_tools() 注册
     — 生产环境死代码 (test_skill.py 用手工 dispatcher 绕过了此问题)。
  2. 即使注册, decompose() 发出的 lint_code 子调用 {"path": ...} 会被
     LintCodeTool.validate() 以 "参数 'code' 是必需的" 拒绝。
  3. dispatcher 只在 run_stream() 注入 — run() 路径缺失。

本文件用 真实注册表 + 真实 ToolRouter 验证完整链路。
"""

import pytest
from unittest.mock import patch

from agent.tools.schema import ToolCall, ToolResult, ToolResultStatus
from agent.tools.skill import Skill


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def orch():
    """已 initialize() 的 orchestrator — 真实 registry + router，仅 mock LLM。"""
    with patch("agent.orchestrator.LLMClient"):
        from agent.orchestrator import AgentOrchestrator
        orch = AgentOrchestrator()
        orch.initialize()
        yield orch
    # 清理: 从全局注册表注销, 避免污染其他测试
    if orch.registry is not None and orch.registry.has("analyze_code"):
        orch.registry.unregister("analyze_code")


@pytest.fixture
def workspace(tmp_path):
    """临时 workspace 根目录 — 保存/恢复全局 workspace root。"""
    from agent.tools.builtin import file_tools
    prev = file_tools._workspace_root
    root = tmp_path / "workspace"
    root.mkdir()
    file_tools.set_workspace_root(root)
    yield root
    file_tools._workspace_root = prev


# ══════════════════════════════════════════════════════════
# (a) 生产注册
# ══════════════════════════════════════════════════════════


class TestProductionRegistration:
    """initialize() 后 analyze_code 已注册且 dispatcher 已注入。"""

    def test_initialize_registers_analyze_code(self, orch):
        """初始化后全局注册表包含可分发的 analyze_code 工具。"""
        tool = orch.registry.get("analyze_code")
        assert tool is not None
        assert isinstance(tool, Skill)
        assert tool.schema.name == "analyze_code"
        assert tool.schema.category == "code"
        assert tool.schema.is_readonly is True

    def test_dispatcher_injected_at_initialize(self, orch):
        """initialize() 注入 router.dispatch — run() 与 run_stream() 共用。"""
        tool = orch.registry.get("analyze_code")
        assert tool is not None
        assert tool._dispatcher is not None
        assert tool._dispatcher == orch.router.dispatch


# ══════════════════════════════════════════════════════════
# (b) 真实链路: dispatch → decompose → 子调用 validate → synthesize
# ══════════════════════════════════════════════════════════


class TestRealRouterPipeline:
    """SkillCall 走真实 ToolRouter 的完整链路。"""

    def test_analyze_code_routes_through_real_router(self, orch, workspace):
        """read_file / lint_code 子调用全部通过 validate()，无 REJECTED。"""
        content = "print('hello')\n"
        py_file = workspace / "sample.py"
        py_file.write_text(content, encoding="utf-8")

        result = orch.router.dispatch(
            ToolCall(tool="analyze_code", input={"path": str(py_file), "task": "分析"})
        )

        assert isinstance(result, ToolResult)
        assert result.status == ToolResultStatus.SUCCESS
        assert result.ok is True
        assert result.tool == "analyze_code"

        sub_results = result.data["sub_results"]
        assert len(sub_results) == 2
        assert [s["tool"] for s in sub_results] == ["read_file", "lint_code"]
        # 关键断言: 子调用全部通过各自工具的 validate()，不允许 REJECTED
        assert all(s["status"] == "success" for s in sub_results)

        analysis = result.data["analysis"]
        assert analysis["file_size_chars"] == len(content)
        assert analysis["lint_issues_count"] == 0
        assert analysis["verdict"] == "pass"

    def test_router_history_records_sub_calls(self, orch, workspace):
        """router 调用历史包含 analyze_code 及两个子调用。"""
        py_file = workspace / "sample.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        orch.router.dispatch(ToolCall(tool="analyze_code", input={"path": str(py_file)}))

        called_tools = {r.tool for r in orch.router.call_history}
        assert called_tools == {"analyze_code", "read_file", "lint_code"}


# ══════════════════════════════════════════════════════════
# (c) 契约修复: LintCodeTool 接受 path
# ══════════════════════════════════════════════════════════


class TestLintCodePathContract:
    """lint_code 的 path 参数契约 — decompose 的输出必须通过 validate()。"""

    def test_lint_code_validate_accepts_path(self, workspace):
        from agent.tools.builtin.code_tools import LintCodeTool
        py_file = workspace / "sample.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        tool = LintCodeTool()
        ok, msg = tool.validate(ToolCall(tool="lint_code", input={"path": str(py_file)}))
        assert ok is True, msg

    def test_lint_code_execute_with_path(self, workspace):
        from agent.tools.builtin.code_tools import LintCodeTool
        py_file = workspace / "sample.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        tool = LintCodeTool()
        result = tool.execute(ToolCall(tool="lint_code", input={"path": str(py_file)}))

        assert result.status == ToolResultStatus.SUCCESS
        assert result.data["syntax_ok"] is True

    def test_lint_code_validate_rejects_empty(self):
        """code 与 path 都没有时仍应拒绝。"""
        from agent.tools.builtin.code_tools import LintCodeTool
        tool = LintCodeTool()
        ok, msg = tool.validate(ToolCall(tool="lint_code", input={}))
        assert ok is False
