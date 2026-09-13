# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""triggers/condition_parser.py 行覆盖缺口补充测试。

聚焦既有 test_triggers.py 未触达的安全求值器分支：下标访问/嵌套 None 链/
浮点字面量/括号分组/列表字面量与路径的语法错误族/比较类型失配兜底/
求值异常兜底。全部经公共入口 compile_condition / parse_condition 断言
可观察行为（求值结果 / 注册期语法错误），不断言 AST 内部结构。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from triggers.condition_parser import (  # noqa: E402
    _SyntaxError_,
    compile_condition,
    parse_condition,
)

pytestmark = pytest.mark.unit


class TestPathResolveGaps:
    """点链/下标取值的回退与兜底分支。"""

    def test_nested_chain_through_none_yields_none(self) -> None:
        """嵌套回退链中途遇 None（根缺失/中间值 None）→ 取值 None，比较为假。"""
        assert parse_condition("a.b == 'x'", {"a": None}) is False
        assert parse_condition("deep.missing.tail == 1", {}) is False
        # 扁平键同名时优先，不进 None 链（对照组）
        assert parse_condition("a.b == 'x'", {"a.b": "x", "a": None}) is True

    def test_index_access_dict_and_list(self) -> None:
        """下标访问：dict 字符串键与 list 整数下标两种合法形态。"""
        assert parse_condition("m['k'] == 'v'", {"m": {"k": "v"}}) is True
        assert parse_condition("xs[1] == 2", {"xs": [1, 2]}) is True
        assert parse_condition("m['nope'] == 'v'", {"m": {"k": "v"}}) is False  # 缺键 → None

    @pytest.mark.parametrize(
        ("expr", "ctx"),
        [
            ("xs[5] == 1", {"xs": [1]}),    # 下标越界
            ("xs['k'] == 1", {"xs": [1]}),  # list 配字符串键
            ("m[0] == 1", {"m": {"a": 1}}),  # dict 配整数键
            ("xs[0] == 1", {"xs": []}),     # 空列表
        ],
        ids=["oob", "list-str-key", "dict-int-key", "empty-list"],
    )
    def test_index_shape_mismatch_is_false(self, expr: str, ctx: dict[str, Any]) -> None:
        """下标与容器形态不匹配 → 安全兜底 None → 比较为假，不抛异常。"""
        assert parse_condition(expr, ctx) is False


class TestNumberLiterals:
    def test_float_literal(self) -> None:
        assert parse_condition("x > 1.5", {"x": 2}) is True
        assert parse_condition("x == 1.5", {"x": 1.5}) is True
        assert parse_condition("x > 2.5", {"x": 2}) is False
        assert parse_condition("x == 1.50", {"x": 1.5}) is True


class TestSyntaxErrorFamily:
    """tokenize/parse 阶段的语法错误族：注册期显式暴露，求值层兜底 False。"""

    def test_single_equals_rejected(self) -> None:
        with pytest.raises(_SyntaxError_, match=r"单个 '='"):
            compile_condition("x = 1")
        with pytest.raises(_SyntaxError_, match=r"单个 '='"):
            compile_condition("name = 'a'")
        assert parse_condition("x = 1", {"x": 1}) is False  # 求值层安全兜底

    @pytest.mark.parametrize("expr", ["a @ b", "a # b", "a ; b"])
    def test_unrecognized_character_rejected(self, expr: str) -> None:
        with pytest.raises(_SyntaxError_, match="无法识别的字符"):
            compile_condition(expr)

    def test_dot_must_be_followed_by_field_name(self) -> None:
        for expr in ("a. == 1", "a.1 == 2"):
            with pytest.raises(_SyntaxError_, match="'.' 后应为字段名"):
                compile_condition(expr)

    def test_leading_unrecognized_token_rejected(self) -> None:
        for expr in (", 1", ") 1", "] 1"):
            with pytest.raises(_SyntaxError_, match="无法识别的 token"):
                compile_condition(expr)

    def test_leading_operator_degrades_false(self) -> None:
        """前导操作符的畸形表达式 → 求值层安全兜底 False（不外抛、不误判真）。"""
        assert parse_condition("== 1", {}) is False
        assert parse_condition("!= true", {}) is False

    def test_parenthesized_group(self) -> None:
        ctx = {"a": 1, "b": 2, "c": 5}
        assert parse_condition("(a == 1 or b == 99) and c > 3", ctx) is True
        assert parse_condition("(a == 99 or b == 2) and c > 99", ctx) is False
        with pytest.raises(_SyntaxError_, match=r"期望 '\)'"):
            compile_condition("(a == 1 b)")

    def test_index_bracket_must_close(self) -> None:
        with pytest.raises(_SyntaxError_, match=r"期望 '\]'"):
            compile_condition("xs[0 == 1]")

    @pytest.mark.parametrize(
        ("expr", "match"),
        [
            ("xs == [1, 2", "列表未闭合"),
            ("xs == [1", "列表未闭合"),
            ("xs == [1 2]", "列表内意外"),
            ("xs == ['a' 'b']", "列表内意外"),
        ],
        ids=["unclosed-tail", "unclosed-single", "missing-comma", "missing-comma-str"],
    )
    def test_list_literal_errors(self, expr: str, match: str) -> None:
        with pytest.raises(_SyntaxError_, match=match):
            compile_condition(expr)


class TestBooleanShortCircuit:
    def test_and_short_circuits_on_false_left(self) -> None:
        assert parse_condition("x == 'a' and y == 'b'", {"x": "b", "y": "b"}) is False
        assert parse_condition("none and true", {}) is False
        assert parse_condition("x == 'a' and y == 'b'", {"x": "a", "y": "b"}) is True  # 真值对照

    def test_or_short_circuits_on_true_left(self) -> None:
        assert parse_condition("x == 'a' or y == 'b'", {"x": "a", "y": "b"}) is True
        assert parse_condition("true or none", {}) is True
        assert parse_condition("x == 'a' or y == 'b'", {"x": "b", "y": "c"}) is False  # 假值对照


class TestCompareFallback:
    @pytest.mark.parametrize(
        ("expr", "ctx"),
        [
            ("s > 1", {"s": "abc"}),  # str > int
            ("n < 'x'", {"n": 1}),    # int < str
            ("missing > 1", {}),      # None > int
        ],
        ids=["str-gt-int", "int-lt-str", "none-gt-int"],
    )
    def test_incomparable_types_false(self, expr: str, ctx: dict[str, Any]) -> None:
        """类型不可比较 → TypeError 安全兜底 False，与引擎语义一致。"""
        assert parse_condition(expr, ctx) is False

    def test_unknown_operator_false(self) -> None:
        """单 '!'（非 '!='）经 tokenizer 成 OP → 比较器未知操作符 → False 兜底。"""
        assert parse_condition("a ! 1", {"a": 1}) is False
        assert parse_condition("a ! 1", {"a": 2}) is False  # 与操作数取值无关恒假

    def test_eval_exception_falls_back_false(self) -> None:
        """求值异常（非 TypeError 的任意异常）→ parse_condition 顶层兜底 False，不外抛。"""

        class _Raising:
            def __gt__(self, other: object) -> bool:
                raise ValueError("boom")

        assert parse_condition("obj > 1", {"obj": _Raising()}) is False
