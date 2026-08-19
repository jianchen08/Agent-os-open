# @feature: FP-0.2.二 科学计算器 | @vision: V2 安全 | @ci: python-coverage
"""calc _safe_eval AST 白名单求值测试（eval 替换——空 builtins 的 eval 可属性链逃逸）。

覆盖：
1. 常规运算/常量/函数行为对齐（三角角度制、log 双参、factorial int 收敛——
   旧字符串替换实现 "))"→")" 误替换使 factorial(5) 语法错误，AST 实现下修复）
2. 安全负样本：属性链逃逸 / __import__ / 未知名称与函数 / 关键字参数 /
   非表达式节点（IfExp、List、字符串常量）一律 ValueError，
   且被 scientific_calculator 收敛为 {"error": ...} 不外抛
3. DoS 防护：超大指数、超大阶乘入参拒绝
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "calc_tools_under_test", Path(__file__).with_name("calc_tools.py")
)
calc = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("calc_tools_under_test", calc)
_SPEC.loader.exec_module(calc)  # type: ignore[union-attr]


def _eval(expr: str) -> float:
    return float(calc._safe_eval(expr))


# ── 1. 行为对齐 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("1+2*3", 7),
        ("2**10", 1024),
        ("7//2", 3),
        ("7%3", 1),
        ("-5+3", -2),
        ("2*pi", 2 * math.pi),
        ("e**2", math.e**2),
        ("sqrt(16)", 4),
        ("sin(30)", 0.5),
        ("cos(60)", 0.5),
        ("log(8, 2)", 3),
        ("log(e)", 1),
        ("abs(-3)", 3),
        ("cbrt(-27)", -3),
        ("ceil(2.1)", 3),
        ("floor(2.9)", 2),
        ("factorial(5)", 120),
        ("factorial(5.0)", 120),
        ("sqrt(4 + 3*4)", 4),
        ("round(2.5)", 2),
    ],
)
def test_eval_behavior(expr: str, expected: float) -> None:
    assert _eval(expr) == pytest.approx(expected)


def test_constants_direct() -> None:
    assert _eval("pi") == pytest.approx(math.pi)
    assert _eval("tau") == pytest.approx(math.tau)
    assert _eval("PI") == pytest.approx(math.pi)  # 旧实现 lower() 大小写不敏感


# ── 2. 安全负样本（一律 ValueError，不允许求值成功）─────────────────────
@pytest.mark.parametrize(
    "expr",
    [
        # 属性链逃逸（空 builtins eval 的经典逃逸面）
        "().__class__.__base__.__subclasses__()",
        "''.__class__.__mro__",
        # 动态导入 / 内建函数
        "__import__('os').system('dir')",
        "open('C:/agentos_kernel.db')",
        "eval('1+1')",
        "exec('pass')",
        # 未知名称 / 未知函数 / 复合函数表达式
        "unknown_name",
        "pow(2, 3)",
        "math.pi",
        "(lambda x: x)(1)",
        # 关键字参数（可注入 log 的 base 绕过签名）
        "log(8, base=2)",
        # 非表达式节点
        "1 if 1 else 2",
        "[1, 2, 3]",
        "'abc'",
        "f'{1}'",
    ],
)
def test_eval_rejects_dangerous(expr: str) -> None:
    with pytest.raises(ValueError):
        calc._safe_eval(expr)


def test_calc_tool_converges_error_dict() -> None:
    """危险表达式经工具入口收敛为 error dict，不外抛异常。"""
    result = asyncio.run(
        calc.scientific_calculator(operation="calculate", expression="__import__('os')")
    )
    assert "error" in result
    assert "__import__" in result["error"]


# ── 3. DoS 防护 ─────────────────────────────────────────────────────────
def test_pow_exponent_cap() -> None:
    with pytest.raises(ValueError, match="指数过大"):
        calc._safe_eval("2**999999999")


def test_factorial_input_cap() -> None:
    with pytest.raises(ValueError, match="阶乘入参过大"):
        calc._safe_eval("factorial(100000000)")
