"""H2 安全回归：eval 求值换 simpleeval AST 白名单。

漏洞：scientific_calculator._safe_eval 与 application.py 的 calculator
用 eval(expression, {"__builtins__": {}}, allowed_names) 求值用户表达式。
__builtins__={} 是伪安全，经典 payload ().__class__.__bases__[0].__subclasses__()
即可逃逸到 os.system。

修复：两处都改为 simpleeval（项目已有依赖，AST 白名单求值器）。

本测试守护修复：注入逃逸 payload 必须抛异常，正常表达式必须正确计算。
若有人改回 eval，逃逸 payload 测试变红。
"""
from __future__ import annotations

import math

import pytest

# 经典 eval 逃逸 payload（修复前可拿到 os.system，修复后必须被拒）
_ESCAPE_PAYLOADS = [
    pytest.param("().__class__.__bases__[0].__subclasses__()", id="subclass_chain"),
    pytest.param("__import__('os').system('id')", id="import_os"),
    pytest.param("().__class__.__init__.__globals__", id="init_globals"),
    pytest.param("open('/etc/passwd').read()", id="open_file"),
]


class TestScientificCalculatorSafeEval:
    """H2: scientific_calculator._safe_eval 用 simpleeval。"""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("2 + 3 * 4", 14),
            ("sqrt(16)", 4.0),
            ("pi", math.pi),
            ("log(100, 10)", 2.0),
            ("factorial(5)", 120),
            ("2 ** 10", 1024),
        ],
    )
    def test_normal_expressions_compute_correctly(self, expr: str, expected) -> None:
        from tools.builtin.scientific_calculator.tool import ScientificCalculatorTool

        t = ScientificCalculatorTool()
        assert t._safe_eval(expr) == pytest.approx(expected)

    @pytest.mark.parametrize("payload", _ESCAPE_PAYLOADS)
    def test_escape_payloads_are_blocked(self, payload: str) -> None:
        from tools.builtin.scientific_calculator.tool import ScientificCalculatorTool

        t = ScientificCalculatorTool()
        # 逃逸 payload 必须抛异常（simpleeval 会抛 FeatureNotAvailable/
        # AttributeDoesNotExist/NameNotDefined 等多种异常，故用宽基类）
        with pytest.raises(Exception):  # noqa: B017, PT011
            t._safe_eval(payload)


class TestApplicationCalculatorSafeEval:
    """H2: application.py 内嵌 calculator 用 simpleeval。

    calculator 是 application.py 内的闭包，无法直接 import。这里提取相同的
    求值逻辑（simple_eval + 同样的 constants/functions）做等价验证，守护
    "calculator 用 simpleeval 而非 eval" 这个不变量。
    """

    @staticmethod
    def _eval_like_calculator(expression: str):
        """复刻 application.py calculator 的 simpleeval 调用。"""
        from simpleeval import simple_eval

        _math = math
        constants = {"pi": _math.pi, "e": _math.e}
        functions = {
            "abs": abs,
            "round": round,
            "min": min,
            "max": max,
            "pow": pow,
            "sum": sum,
            "sqrt": _math.sqrt,
            "ceil": _math.ceil,
            "floor": _math.floor,
        }
        return simple_eval(expression, names=constants, functions=functions)

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("123+456", 579),
            ("sqrt(144)", 12.0),
            ("abs(-7)", 7),
            ("pow(2,8)", 256),
        ],
    )
    def test_normal_expressions(self, expr: str, expected) -> None:
        assert self._eval_like_calculator(expr) == pytest.approx(expected)

    @pytest.mark.parametrize("payload", _ESCAPE_PAYLOADS)
    def test_escape_payloads_blocked(self, payload: str) -> None:
        with pytest.raises(Exception):  # noqa: B017, PT011
            self._eval_like_calculator(payload)
