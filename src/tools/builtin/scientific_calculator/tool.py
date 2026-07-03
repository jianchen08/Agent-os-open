"""
科学计算器工具

暴露接口：
- create_scientific_calculator_tool() -> ScientificCalculatorTool：创建工具实例
- get_tool_definition() -> Tool：获取工具定义
- ScientificCalculatorTool：科学计算器工具类
"""

import logging
import math
from typing import Any, Union  # noqa: F401

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class ScientificCalculatorTool(BuiltinTool):
    """
    科学计算器工具

    支持三角函数、对数、幂运算、常量等科学计算功能
    """

    # 数学常量映射
    CONSTANTS = {
        "pi": math.pi,
        "e": math.e,
        "tau": math.tau,
        "inf": math.inf,
    }

    # 支持的运算操作映射
    OPERATIONS = {
        # 三角函数
        "sin": lambda x: math.sin(math.radians(x)),
        "cos": lambda x: math.cos(math.radians(x)),
        "tan": lambda x: math.tan(math.radians(x)),
        "asin": lambda x: math.degrees(math.asin(x)),
        "acos": lambda x: math.degrees(math.acos(x)),
        "atan": lambda x: math.degrees(math.atan(x)),
        # 双曲三角函数
        "sinh": math.sinh,
        "cosh": math.cosh,
        "tanh": math.tanh,
        # 对数函数
        "log": lambda x, base: math.log(x, base) if base else math.log(x),
        "ln": math.log,
        "log10": math.log10,
        "log2": math.log2,
        # 幂和根
        "pow": math.pow,
        "sqrt": math.sqrt,
        "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
        # 其他数学函数
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

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="scientific_calculator",
            description="科学计算器工具，支持三角函数、对数、幂运算、阶乘等高级数学运算。"
            "适用场景：需要进行科学计算、复杂数学表达式求值、统计分析中的数学运算。"
            "不适用场景：简单的加减乘除（使用代码执行器更高效）、符号数学运算（使用专用符号计算库）。"
            "支持操作：sin/cos/tan（角度制）、asin/acos/atan（反三角函数，返回角度）、"
            "sinh/cosh/tanh（双曲三角函数）、log/ln（对数）、log10/log2（常用/二进制对数）、"
            "pow（幂运算）、sqrt/cbrt（平方/立方根）、abs/ceil/floor/round（取整）、"
            "factorial（阶乘）、gcd（最大公约数）、exp（指数）、degrees/radians（角度转换）。"
            "支持常量：pi、e、tau、inf。",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "description": "运算类型，可选值：calculate（计算表达式）、evaluate（求值单个操作）",
                        "enum": ["calculate", "evaluate"],
                    },
                    "expression": {
                        "type": "string",
                        "description": "数学表达式（operation为calculate时使用）。支持运算符：+、-、*、/、**、%。"
                        "示例：'2 + 3 * 4'、'sin(30) + cos(60)'、'sqrt(16) + log(100, 10)'",
                    },
                    "func": {
                        "type": "string",
                        "description": "数学函数名（operation为evaluate时使用）。"
                        "可选：sin, cos, tan, asin, acos, atan, sinh, cosh, tanh, "
                        "log, ln, log10, log2, pow, sqrt, cbrt, abs, ceil, floor, "
                        "round, factorial, gcd, exp, degrees, radians",
                    },
                    "value": {"description": "运算值（单参数函数使用）。数值类型。"},
                    "values": {
                        "type": "array",
                        "description": "运算值数组（双参数函数如pow、log使用）。格式：[底数, 指数]或[数值, 底数]",
                        "items": {"type": "number"},
                    },
                },
                "required": ["operation"],
                "oneOf": [
                    {"required": ["expression"]},
                    {"required": ["func", "value"]},
                    {"required": ["func", "values"]},
                ],
            },
            source=ToolSource.CODE,
            category=ToolCategory.ANALYSIS,
            level=ToolLevel.SYSTEM,
            tags=["calculator", "math", "science", "analysis"],
        )

    def _evaluate_single_operation(self, func: str, value: int | float, values: list = None) -> int | float:
        """求值单个数学函数"""
        # 处理常量
        if func.lower() in self.CONSTANTS:
            if value is not None:
                # 常量与数值运算
                const_val = self.CONSTANTS[func.lower()]
                return const_val * value if value != 1 else const_val
            return self.CONSTANTS[func.lower()]

        # 处理运算函数
        if func.lower() not in self.OPERATIONS:
            raise ValueError(f"不支持的函数: {func}")

        op_func = self.OPERATIONS[func.lower()]

        # 双参数函数
        if func.lower() in ("pow", "log", "gcd"):
            if values and len(values) >= 2:
                return op_func(values[0], values[1])
            raise ValueError(f"函数 {func} 需要两个参数")

        # 对数函数带底数
        if func.lower() in ("log",):
            if values and len(values) >= 2:
                return math.log(values[0], values[1])
            # log默认为自然对数
            return op_func(value)

        # 单参数函数
        return op_func(value)

    def _safe_eval(self, expression: str) -> int | float:
        """安全地计算数学表达式"""
        # 替换常量
        expr = expression.lower()
        for name, val in self.CONSTANTS.items():
            expr = expr.replace(name, str(val))

        # 安全替换数学函数（使用math模块）
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
            expr = expr.replace(name + "(", pattern % "")
            # 修正已替换的模式（末尾的 % "" 会留下多余括号）
            expr = expr.replace("))", ")")

        # 使用 eval 计算表达式（仅允许数学运算）
        allowed_names = {
            "math": math,
            "abs": abs,
            "round": round,
        }

        result = eval(expr, {"__builtins__": {}}, allowed_names)
        return result

    async def execute(  # noqa: PLR0911,PLR0912
        self,
        inputs: dict[str, Any],
        context: Any = None,
    ) -> ToolExecutionResult:
        """执行科学计算"""
        operation = inputs.get("operation", "evaluate")

        try:
            if operation == "calculate":
                # 计算数学表达式
                expression = inputs.get("expression", "")
                if not expression:
                    return create_failure_result(
                        error="表达式不能为空",
                        metadata={"action": "scientific_calculator"},
                    )

                logger.info("[科学计算器] 计算表达式: %s", expression)
                result = self._safe_eval(expression)

                # 格式化结果
                if isinstance(result, float):
                    result = int(result) if result == int(result) else round(result, 10)

                return create_success_result(
                    data={
                        "expression": expression,
                        "result": result,
                    },
                    metadata={"action": "scientific_calculator"},
                )

            if operation == "evaluate":
                # 求值单个函数
                func = inputs.get("func")
                value = inputs.get("value")
                values = inputs.get("values")

                if not func:
                    return create_failure_result(
                        error="函数名不能为空",
                        metadata={"action": "scientific_calculator"},
                    )

                logger.info("[科学计算器] 求值函数: %s", func)

                # 检查是否为常量
                if func.lower() in self.CONSTANTS and value is None:
                    result = self.CONSTANTS[func.lower()]
                else:
                    if value is None and not values:
                        return create_failure_result(
                            error="需要提供value或values参数",
                            metadata={"action": "scientific_calculator"},
                        )
                    result = self._evaluate_single_operation(func, value, values)

                # 格式化结果
                if isinstance(result, float):
                    if math.isnan(result):
                        return create_failure_result(
                            error="计算结果为非数值（NaN）",
                            metadata={"action": "scientific_calculator"},
                        )
                    if math.isinf(result):
                        return create_failure_result(
                            error="计算结果为无穷大",
                            metadata={"action": "scientific_calculator"},
                        )
                    result = int(result) if result == int(result) else round(result, 10)

                return create_success_result(
                    data={
                        "function": func,
                        "input": value if value is not None else values,
                        "result": result,
                    },
                    metadata={"action": "scientific_calculator"},
                )

            return create_failure_result(
                error=f"不支持的运算类型: {operation}",
                metadata={"action": "scientific_calculator"},
            )

        except ZeroDivisionError:
            logger.error("[科学计算器] 除零错误")
            return create_failure_result(
                error="除数不能为零",
                metadata={"action": "scientific_calculator"},
            )

        except ValueError as e:
            logger.error("[科学计算器] 值错误: %s", e)
            return create_failure_result(
                error=f"计算错误: {str(e)}",
                metadata={"action": "scientific_calculator"},
            )

        except Exception as e:
            logger.error("[科学计算器] 计算失败: %s", e)
            return create_failure_result(
                error=f"计算失败: {str(e)}",
                metadata={"action": "scientific_calculator"},
            )


def create_scientific_calculator_tool() -> ScientificCalculatorTool:
    """创建科学计算器工具实例"""
    return ScientificCalculatorTool()


__all__ = ["ScientificCalculatorTool", "create_scientific_calculator_tool"]
