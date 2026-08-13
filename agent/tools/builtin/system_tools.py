"""
系统工具 — 时间查询 / 计算器
"""
import ast
import operator
import re
from datetime import datetime

from agent.tools.schema import ToolProtocol, ToolSchema, ToolCall, ToolResult, ToolResultStatus


class GetTimeTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_current_time",
            description="获取当前日期和时间。",
            parameters={"type": "object", "properties": {}, "required": []},
            category="system",
            tags=["time", "system"],
            is_readonly=True,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.call_id, tool=call.tool,
            status=ToolResultStatus.SUCCESS,
            data={"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "iso": datetime.now().isoformat()},
        )


class CalculatorTool(ToolProtocol):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="calculator",
            description="安全地计算数学表达式。支持 +、-、*、/、**、()、负数。",
            parameters={
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式，如 '2+3*4' 或 '(3**2 + 4**2)**0.5'"},
                },
                "required": ["expression"],
            },
            category="system",
            tags=["math", "calculate"],
            is_readonly=True,
        )

    def validate(self, call: ToolCall) -> tuple[bool, str]:
        expr = call.input.get("expression", "")
        if not expr:
            return False, "参数 'expression' 是必需的"
        forbidden = ["import", "exec", "eval", "__", "open", "os.", "sys.", "subprocess"]
        if any(kw in expr.lower() for kw in forbidden):
            return False, f"安全限制: 表达式包含不允许的关键词"
        return True, "ok"

    def execute(self, call: ToolCall) -> ToolResult:
        expression = call.input["expression"]
        cleaned = expression.strip()
        cleaned = cleaned.replace("×", "*").replace("÷", "/")
        cleaned = cleaned.replace("＋", "+").replace("－", "-")
        cleaned = re.sub(r"[^0-9+\-*/().%^ eEpPiI]", "", cleaned)
        cleaned = cleaned.replace(" ", "")
        cleaned = cleaned.replace("^", "**")

        if not cleaned:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error="表达式为空")

        allowed_ops = {
            ast.Add: operator.add, ast.Sub: operator.sub,
            ast.Mult: operator.mul, ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv, ast.Pow: operator.pow,
            ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
        }

        # 指数上界：防止 9**9**9**9 之类的指数爆炸把进程挂死 / 耗尽内存（DoS）。
        # 在实际求值前拦截，right 是已求值的指数，abs 超过阈值即拒绝。
        MAX_EXPONENT = 1000

        try:
            tree = ast.parse(cleaned, mode="eval")

            def eval_node(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                    return node.value
                if isinstance(node, ast.BinOp) and type(node.op) in allowed_ops:
                    left = eval_node(node.left)
                    right = eval_node(node.right)
                    if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                        raise ValueError(f"指数超出安全上界（|exp| ≤ {MAX_EXPONENT}）")
                    return allowed_ops[type(node.op)](left, right)
                if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_ops:
                    return allowed_ops[type(node.op)](eval_node(node.operand))
                raise ValueError("暂不支持该表达式")

            result = eval_node(tree.body)
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.SUCCESS, data={"result": result, "expression": expression})
        except Exception as e:
            return ToolResult(call_id=call.call_id, tool=call.tool, status=ToolResultStatus.ERROR, error=f"计算失败: {e}")
