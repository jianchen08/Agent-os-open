"""科学计算器工具——从 0.1 提取核心逻辑为纯函数。

[来源: src/tools/builtin/scientific_calculator/tool.py]
"""

from __future__ import annotations

import math
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

_OPERATIONS = {
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
    """安全地计算数学表达式。"""
    expr = expression.lower()
    for name, val in _CONSTANTS.items():
        expr = expr.replace(name, str(val))

    safe_funcs = {
        "sin": "math.sin(math.radians(%s))",
        "cos": "math.cos(math.radians(%s))",
        "tan": "math.tan(math.radians(%s))",
        "asin": "math.degrees(math.asin(%s))",
        "acos": "math.degrees(math.acos(%s))",
        "atan": "math.degrees(math.atan(%s))",
        "sinh": "math.sinh(%s)",
        "cosh": "math.cosh(%s)",
        "tanh": "math.tanh(%s)",
        "log": "math.log(%s)",
        "ln": "math.log(%s)",
        "log10": "math.log10(%s)",
        "log2": "math.log2(%s)",
        "sqrt": "math.sqrt(%s)",
        "cbrt": "math.copysign(abs(%s)**(1/3), %s)",
        "abs": "abs(%s)",
        "ceil": "math.ceil(%s)",
        "floor": "math.floor(%s)",
        "factorial": "math.factorial(int(%s))",
        "exp": "math.exp(%s)",
        "degrees": "math.degrees(%s)",
        "radians": "math.radians(%s)",
    }

    for name, pattern in safe_funcs.items():
        # 使用占位符替换避免 % 格式化问题（cbrt 有两个 %s）
        expr = expr.replace(name + "(", pattern.replace("%s", ""))
        # 修正替换后多余的双右括号
    expr = expr.replace("))", ")")

    allowed_names = {"math": math, "abs": abs, "round": round}
    result = eval(expr, {"__builtins__": {}}, allowed_names)  # noqa: S307
    return result


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
