"""科学计算器工具——从 0.1 提取核心逻辑为纯函数。

[来源: src/tools/builtin/scientific_calculator/tool.py]
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

SCIENTIFIC_CALCULATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["calculate", "evaluate"],
            "description": "运算类型：calculate（计算表达式）、evaluate（求值单个操作）",
        },
        "expression": {
            "type": "string",
            "description": "数学表达式（operation为calculate时使用）",
        },
        "func": {
            "type": "string",
            "description": "数学函数名（operation为evaluate时使用）",
        },
        "value": {"description": "运算值（单参数函数使用）"},
        "values": {
            "type": "array",
            "items": {"type": "number"},
            "description": "运算值数组（双参数函数如pow、log使用）",
        },
    },
    "required": ["operation"],
}

_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

# 值混合了一元/二元 lambda 与 math 内置函数（调用处按函数名约定参数个数），
# 声明为 Any 以避免 mypy 对异构可调用联合的 unknown-type 调用报错。
_OPERATIONS: dict[str, Any] = {
    "sin": lambda x: math.sin(math.radians(x)),
    "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)),
    "asin": lambda x: math.degrees(math.asin(x)),
    "acos": lambda x: math.degrees(math.acos(x)),
    "atan": lambda x: math.degrees(math.atan(x)),
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "log": lambda x, base: math.log(x, base) if base else math.log(x),
    "ln": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "pow": math.pow,
    "sqrt": math.sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "round": round,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "exp": math.exp,
    "degrees": math.degrees,
    "radians": math.radians,
}


def _evaluate_single(func: str, value: Any = None, values: list | None = None) -> int | float:
    """求值单个数学函数。"""
    if func.lower() in _CONSTANTS:
        if value is not None:
            const_val = _CONSTANTS[func.lower()]
            return const_val * value if value != 1 else const_val
        return _CONSTANTS[func.lower()]

    if func.lower() not in _OPERATIONS:
        raise ValueError(f"不支持的函数: {func}")

    op_func = _OPERATIONS[func.lower()]

    if func.lower() in ("pow", "log", "gcd"):
        if values and len(values) >= 2:
            return op_func(values[0], values[1])
        raise ValueError(f"函数 {func} 需要两个参数")

    return op_func(value)


def _safe_eval(expression: str) -> int | float:
    """安全地计算数学表达式（AST 白名单节点求值）。

    替换旧实现的 eval：空 ``__builtins__`` 的 eval 可经
    ``().__class__.__base__.__subclasses__()`` 属性链逃逸执行任意代码，
    AST 白名单只放行数值常量/算子/白名单函数调用，属性访问与非数值节点一律拒绝。
    """
    expr = expression.lower()
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e}") from e
    return _ast_eval(tree)


# DoS 防护：限制幂运算指数与阶乘入参的量级（2**99999999 / factorial(10**8) 可挂死进程）
_MAX_POW_EXPONENT = 10_000
_MAX_FACTORIAL_INPUT = 100_000


def _guarded_factorial(x: Any) -> int:
    n = int(x)
    if abs(n) > _MAX_FACTORIAL_INPUT:
        raise ValueError(f"阶乘入参过大: {n}")
    return math.factorial(n)


# 表达式上下文函数白名单（与旧字符串替换实现行为对齐：三角函数角度制、
# log 支持单/双参、factorial 收敛 int——旧实现 "))"→")" 误替换使 factorial 语法错误，此处修复）
_EXPR_FUNCS: dict[str, Any] = {
    "sin": lambda x: math.sin(math.radians(x)),
    "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)),
    "asin": lambda x: math.degrees(math.asin(x)),
    "acos": lambda x: math.degrees(math.acos(x)),
    "atan": lambda x: math.degrees(math.atan(x)),
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "log": lambda x, base=None: math.log(x, base) if base is not None else math.log(x),
    "ln": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "sqrt": math.sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "round": round,
    "factorial": _guarded_factorial,
    "exp": math.exp,
    "degrees": math.degrees,
    "radians": math.radians,
}

_EXPR_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_EXPR_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

def _ast_eval(node: ast.AST) -> int | float:
    """AST 白名单节点递归求值。"""
    if isinstance(node, ast.Expression):
        return _ast_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"不支持的常量: {node.value!r}")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _EXPR_BIN_OPS:
        left = _ast_eval(node.left)
        right = _ast_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError(f"指数过大: {right}")
        return _EXPR_BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _EXPR_UNARY_OPS:
        return _EXPR_UNARY_OPS[type(node.op)](_ast_eval(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"不支持的名称: {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _EXPR_FUNCS:
            desc = getattr(node.func, "id", None)
            raise ValueError(f"不支持的函数: {desc or '复合表达式'}")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        args = [_ast_eval(a) for a in node.args]
        return _EXPR_FUNCS[node.func.id](*args)
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


async def scientific_calculator(
    operation: str,
    expression: str = "",
    func: str = "",
    value: Any = None,
    values: list | None = None,
) -> dict[str, Any]:
    """科学计算器。"""
    try:
        if operation == "calculate":
            if not expression:
                return {"error": "表达式不能为空"}
            result = _safe_eval(expression)
            if isinstance(result, float):
                result = int(result) if result == int(result) else round(result, 10)
            return {"expression": expression, "result": result}

        if operation == "evaluate":
            if not func:
                return {"error": "函数名不能为空"}
            if func.lower() in _CONSTANTS and value is None:
                result = _CONSTANTS[func.lower()]
            else:
                if value is None and not values:
                    return {"error": "需要提供value或values参数"}
                result = _evaluate_single(func, value, values)
            if isinstance(result, float):
                if math.isnan(result):
                    return {"error": "计算结果为非数值（NaN）"}
                if math.isinf(result):
                    return {"error": "计算结果为无穷大"}
                result = int(result) if result == int(result) else round(result, 10)
            return {
                "function": func,
                "input": value if value is not None else values,
                "result": result,
            }

        return {"error": f"不支持的运算类型: {operation}"}
    except ZeroDivisionError:
        return {"error": "除数不能为零"}
    except ValueError as e:
        return {"error": f"计算错误: {e}"}
    except Exception as e:
        return {"error": f"计算失败: {e}"}
