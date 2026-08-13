"""
测试系统工具 — GetTimeTool / CalculatorTool。

重点覆盖 calculator 的安全边界（指数 DoS、关键词过滤、AST 求值），
此前该模块零测试（见 testing/report.md 缺口 #7、security M1）。
"""

import pytest

from agent.tools.builtin.system_tools import GetTimeTool, CalculatorTool
from agent.tools.schema import ToolCall, ToolResultStatus


# ── GetTimeTool ────────────────────────────────────────────


def test_get_time_returns_success_with_iso():
    tool = GetTimeTool()
    result = tool.execute(ToolCall(tool="get_current_time", input={}))
    assert result.status == ToolResultStatus.SUCCESS
    assert "time" in result.data
    assert "iso" in result.data


def test_get_time_schema_name():
    assert GetTimeTool().schema.name == "get_current_time"


# ── CalculatorTool: 正常求值 ───────────────────────────────


@pytest.fixture
def calc():
    return CalculatorTool()


@pytest.mark.parametrize("expr,expected", [
    ("2+3*4", 14),
    ("(3**2 + 4**2)**0.5", 5.0),
    ("-7 + 2", -5),
    ("10 / 4", 2.5),
    ("17 % 5", 2),
    ("2 ** 10", 1024),
    ("3 ** 0.5", 3 ** 0.5),
])
def test_calculator_valid_expressions(calc, expr, expected):
    result = calc.execute(ToolCall(tool="calculator", input={"expression": expr}))
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["result"] == pytest.approx(expected)


def test_calculator_unicode_operators(calc):
    # × ÷ 全角符号应被规范化
    result = calc.execute(ToolCall(tool="calculator", input={"expression": "6 × 7"}))
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["result"] == 42


def test_calculator_caret_as_power(calc):
    result = calc.execute(ToolCall(tool="calculator", input={"expression": "2^8"}))
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["result"] == 256


# ── CalculatorTool: validate 关键词过滤 ────────────────────


@pytest.mark.parametrize("bad", [
    "__import__('os')",
    "import os",
    "eval('1')",
    "open('x')",
    "os.system('ls')",
    "subprocess.run(['ls'])",
])
def test_calculator_validate_rejects_dangerous_keywords(calc, bad):
    ok, _ = calc.validate(ToolCall(tool="calculator", input={"expression": bad}))
    assert ok is False


def test_calculator_validate_requires_expression(calc):
    ok, _ = calc.validate(ToolCall(tool="calculator", input={"expression": ""}))
    assert ok is False


# ── CalculatorTool: 指数 DoS 上界（security M1）─────────────
# 这些用例在加上界之前必然挂住/耗尽内存，是本次修复的 red→green 核心。


def test_calculator_rejects_exponent_bomb(calc):
    """9**9**9**9 不得实际求值 —— 必须在计算前被上界拦截。"""
    result = calc.execute(ToolCall(tool="calculator", input={"expression": "9**9**9**9"}))
    assert result.status == ToolResultStatus.ERROR


def test_calculator_rejects_large_single_exponent(calc):
    """单个超大指数（2**100000）也应被拒。"""
    result = calc.execute(ToolCall(tool="calculator", input={"expression": "2**100000"}))
    assert result.status == ToolResultStatus.ERROR


def test_calculator_allows_reasonable_exponent(calc):
    """合理指数（2**64）仍应正常计算，上界不能误伤。"""
    result = calc.execute(ToolCall(tool="calculator", input={"expression": "2**64"}))
    assert result.status == ToolResultStatus.SUCCESS
    assert result.data["result"] == 2 ** 64


# ── CalculatorTool: 求值错误处理 ───────────────────────────


def test_calculator_rejects_unsupported_node(calc):
    # 变量名不是常量/运算，eval_node 应拒绝
    result = calc.execute(ToolCall(tool="calculator", input={"expression": "pi"}))
    assert result.status == ToolResultStatus.ERROR


def test_calculator_empty_after_cleaning(calc):
    result = calc.execute(ToolCall(tool="calculator", input={"expression": "@@@"}))
    assert result.status == ToolResultStatus.ERROR
