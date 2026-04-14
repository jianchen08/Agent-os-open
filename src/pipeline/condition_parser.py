"""安全条件表达式解析器。

替换 eval() 用于路由条件的求值。支持比较操作、布尔逻辑和 state 字段访问，
不使用 eval/exec/compile，杜绝代码注入风险。

支持的条件语法：
    True / False                          — 布尔字面量
    raw_tool_calls != []                  — 变量比较（变量名从 context 查找）
    state["error_count"] > 3              — 下标访问
    state["has_tool_calls"] == True       — 布尔比较
    state["error"] is_empty               — 空值检查
    state["error"] is_not_empty           — 非空检查
    a == 1 and b != 2                     — 布尔 and
    a == 1 or b == 2                      — 布尔 or
    not a                                 — 布尔取反
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_TOKEN_PATTERNS = [
    ("STRING", r'"[^"]*"|\'[^\']*\''),
    ("NUMBER", r"-?\d+\.?\d*"),
    ("BOOL", r"\bTrue\b|\bFalse\b"),
    ("KEYWORD", r"\band\b|\bor\b|\bnot\b|\bin\b|\bis_empty\b|\bis_not_empty\b|\bnot_in\b"),
    ("OP", r"!=|==|>=|<=|>|<"),
    ("BRACKET", r"\[|\]"),
    ("DOT", r"\."),
    ("COMMA", r","),
    ("IDENT", r"[a-zA-Z_][a-zA-Z0-9_]*"),
    ("SKIP", r"\s+"),
]

_TOKEN_RE = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_PATTERNS)
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    """将条件表达式字符串分词。

    Args:
        expr: 条件表达式字符串

    Returns:
        (token_type, token_value) 元组列表
    """
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(expr):
        kind = m.lastgroup
        value = m.group()
        if kind == "SKIP":
            continue
        tokens.append((kind, value))
    return tokens


class _Parser:
    """递归下降条件表达式解析器。"""

    def __init__(self, tokens: list[tuple[str, str]], context: dict[str, Any]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._context = context

    def _peek(self) -> tuple[str, str] | None:
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None

    def _advance(self) -> tuple[str, str]:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, kind: str) -> str:
        tok = self._peek()
        if tok is None or tok[0] != kind:
            raise ValueError(f"Expected {kind}, got {tok}")
        return self._advance()[1]

    def parse(self) -> Any:
        result = self._parse_or()
        return result

    def _parse_or(self) -> Any:
        left = self._parse_and()
        while self._peek() and self._peek()[1] == "or":
            self._advance()
            right = self._parse_and()
            left = bool(left) or bool(right)
        return left

    def _parse_and(self) -> Any:
        left = self._parse_not()
        while self._peek() and self._peek()[1] == "and":
            self._advance()
            right = self._parse_not()
            left = bool(left) and bool(right)
        return left

    def _parse_not(self) -> Any:
        if self._peek() and self._peek()[1] == "not":
            self._advance()
            operand = self._parse_not()
            return not bool(operand)
        return self._parse_comparison()

    def _parse_comparison(self) -> Any:
        left = self._parse_primary()
        tok = self._peek()
        if tok is None:
            return left

        if tok[0] == "OP":
            op = self._advance()[1]
            right = self._parse_primary()
            return self._compare(left, op, right)

        if tok[1] == "is_empty":
            self._advance()
            return left is None or left == "" or left == [] or left == {}

        if tok[1] == "is_not_empty":
            self._advance()
            return left is not None and left != "" and left != [] and left != {}

        if tok[1] == "in":
            self._advance()
            right = self._parse_primary()
            return left in right

        if tok[1] == "not_in":
            self._advance()
            right = self._parse_primary()
            return left not in right

        return left

    def _compare(self, left: Any, op: str, right: Any) -> bool:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
        raise ValueError(f"Unknown operator: {op}")

    def _parse_primary(self) -> Any:
        tok = self._peek()
        if tok is None:
            raise ValueError("Unexpected end of expression")

        if tok[0] == "BOOL":
            return self._advance()[1] == "True"

        if tok[0] == "NUMBER":
            value = self._advance()[1]
            if "." in value:
                return float(value)
            return int(value)

        if tok[0] == "STRING":
            return self._advance()[1][1:-1]

        if tok[0] == "BRACKET" and tok[1] == "[":
            return self._parse_list()

        if tok[0] == "IDENT":
            name = self._advance()[1]
            value = self._resolve_name(name)
            while self._peek() and self._peek()[0] == "BRACKET" and self._peek()[1] == "[":
                self._advance()
                key = self._parse_primary()
                self._expect("BRACKET")
                if isinstance(value, dict) and isinstance(key, (str, int)):
                    value = value.get(key)
                else:
                    value = None
            return value

        raise ValueError(f"Unexpected token: {tok}")

    def _parse_list(self) -> list[Any]:
        self._expect("BRACKET")
        items: list[Any] = []
        while self._peek() and not (self._peek()[0] == "BRACKET" and self._peek()[1] == "]"):
            items.append(self._parse_primary())
            if self._peek() and self._peek()[0] == "COMMA":
                self._advance()
        self._expect("BRACKET")
        return items

    def _resolve_name(self, name: str) -> Any:
        if name in self._context:
            return self._context[name]
        if name == "state":
            return self._context
        return None


def parse_condition(expr: str, context: dict[str, Any]) -> bool:
    """安全地解析并求值条件表达式。

    替换 eval() 用于路由条件的求值。表达式中的标识符从 context 字典中查找。

    Args:
        expr: 条件表达式字符串
        context: 求值上下文（通常是管道 state 字典）

    Returns:
        条件求值结果，解析失败时返回 False
    """
    expr = expr.strip()
    if not expr:
        return True

    try:
        tokens = _tokenize(expr)
        if not tokens:
            return True
        parser = _Parser(tokens, context)
        result = parser.parse()
        return bool(result)
    except Exception:
        logger.debug("Condition parse failed: %s", expr, exc_info=True)
        return False
