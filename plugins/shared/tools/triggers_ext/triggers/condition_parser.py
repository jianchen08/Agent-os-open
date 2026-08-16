"""安全条件表达式求值器（GAP-2）。

`kernel/crates/engine/src/condition.rs` 的 Python 回移（0.1 ``src/pipeline/
condition_parser.py`` 已随 0.1 删除，本文件是它在插件侧的权威实现）。
两段式结构：tokenize → 递归下降 parse 成 AST → 对 context 求值，
不使用任何动态求值（无 eval/exec），杜绝代码注入。

相比 Rust 版的扩展：**扁平点号键优先**。STATE_SUMMARY_KEYS / 任务域字段
（``task.status``、``lineage.root``）在 state 聚合行里是顶层扁平键，
解析点链时先按完整点号路径查扁平键，未命中再走嵌套 dict 逐级访问——
两种形态（扁平键 / 嵌套结构）都能表达。

支持语法（与 Rust 版一致）：
    True / False / None                — 字面量（大小写不敏感）
    'str' / "str" / 123 / 1.5          — 字符串 / 数字
    a.b.c == 'x'                       — 点链访问（扁平键优先，嵌套回退）
    xs != [] / xs == [1, 2]            — 列表比较
    == != > < >= <=                    — 比较
    and / or / not / ( ... )           — 布尔逻辑（优先级 or < and < not）

优先级（低到高）：or < and < not < comparison < primary。
"""

from __future__ import annotations

import enum
from typing import Any


class _TokKind(enum.Enum):
    STRING = "string"
    NUMBER = "number"
    BOOL = "bool"  # true / false / none
    KEYWORD = "keyword"  # and / or / not
    OP = "op"  # == != > < >= <=
    DOT = "dot"
    IDENT = "ident"
    LBRACKET = "["
    RBRACKET = "]"
    LPAREN = "("
    RPAREN = ")"
    COMMA = ","


class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: _TokKind, value: str, pos: int) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos


class _SyntaxError_(Exception):
    """条件表达式语法错误（求值层兜底为 False，注册层可提前暴露）。"""


# ── AST 节点（求值零解析） ──────────────────────────────────────


class _Literal:
    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class _List:
    __slots__ = ("items",)

    def __init__(self, items: list[Any]) -> None:
        self.items = items


class _Path:
    """点链/下标访问：扁平键优先（STATE_SUMMARY_KEYS 约定），嵌套回退。"""

    __slots__ = ("root", "steps")

    def __init__(self, root: str, steps: list[tuple[str, Any]]) -> None:
        self.root = root
        self.steps = steps  # (kind, payload)：("field", name) / ("index", expr)

    def resolve(self, context: dict[str, Any]) -> Any:
        current: Any = context.get(self.root)
        if not self.steps:
            return current
        # 扁平键优先：完整点号路径作为单一键（task.status / a.b.c）
        if all(kind == "field" for kind, _ in self.steps):
            flat_key = ".".join([self.root] + [str(p) for _, p in self.steps])
            if flat_key in context:
                return context[flat_key]
        # 嵌套回退：逐级 dict/下标访问，缺失即 None
        for kind, payload in self.steps:
            if current is None:
                return None
            if kind == "field":
                current = current.get(payload) if isinstance(current, dict) else None
            else:
                key = _eval_value(payload, context)
                if isinstance(current, dict) and isinstance(key, str):
                    current = current.get(key)
                elif isinstance(current, list) and isinstance(key, int) and 0 <= key < len(current):
                    current = current[key]
                else:
                    return None
        return current


class _Not:
    __slots__ = ("operand",)

    def __init__(self, operand: Any) -> None:
        self.operand = operand


class _And:
    __slots__ = ("left", "right")

    def __init__(self, left: Any, right: Any) -> None:
        self.left = left
        self.right = right


class _Or:
    __slots__ = ("left", "right")

    def __init__(self, left: Any, right: Any) -> None:
        self.left = left
        self.right = right


class _Compare:
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: Any, right: Any) -> None:
        self.op = op
        self.left = left
        self.right = right


# ── tokenizer ───────────────────────────────────────────────


def _tokenize(expr: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c in ("'", '"'):
            start = i + 1
            i += 1
            while i < n and expr[i] != c:
                i += 1
            if i >= n:
                raise _SyntaxError_(f"position {start}: 字符串字面量未闭合")
            tokens.append(_Token(_TokKind.STRING, expr[start:i], start))
            i += 1
            continue
        if c.isdigit():
            start = i
            while i < n and expr[i].isdigit():
                i += 1
            if i < n and expr[i] == "." and i + 1 < n and expr[i + 1].isdigit():
                i += 1
                while i < n and expr[i].isdigit():
                    i += 1
            tokens.append(_Token(_TokKind.NUMBER, expr[start:i], start))
            continue
        if c.isalpha() or c == "_":
            start = i
            while i < n and (expr[i].isalnum() or expr[i] == "_"):
                i += 1
            word = expr[start:i]
            lower = word.lower()
            if lower in ("true", "false", "none"):
                tokens.append(_Token(_TokKind.BOOL, lower, start))
            elif lower in ("and", "or", "not"):
                tokens.append(_Token(_TokKind.KEYWORD, lower, start))
            else:
                tokens.append(_Token(_TokKind.IDENT, word, start))
            continue
        if c in "=!<>":
            if i + 1 < n and expr[i + 1] == "=":
                tokens.append(_Token(_TokKind.OP, f"{c}=", start))
                i += 2
                continue
            if c == "=":
                raise _SyntaxError_(f"position {i}: 单个 '=' 不合法（应为 '=='）")
            tokens.append(_Token(_TokKind.OP, c, start))
            i += 1
            continue
        single = {
            ".": _TokKind.DOT,
            "[": _TokKind.LBRACKET,
            "]": _TokKind.RBRACKET,
            "(": _TokKind.LPAREN,
            ")": _TokKind.RPAREN,
            ",": _TokKind.COMMA,
        }.get(c)
        if single is None:
            raise _SyntaxError_(f"position {i}: 无法识别的字符 '{c}'")
        tokens.append(_Token(single, c, i))
        i += 1
    return tokens


# ── parser（递归下降） ────────────────────────────────────────


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> _Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self) -> _Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> Any:
        ast = self._parse_or()
        if self.pos != len(self.tokens):
            raise _SyntaxError_(f"position {self.pos}: 多余的 token '{self.tokens[self.pos].value}'")
        return ast

    def _parse_or(self) -> Any:
        left = self._parse_and()
        while (tok := self._peek()) and tok.kind == _TokKind.KEYWORD and tok.value == "or":
            self._advance()
            left = _Or(left, self._parse_and())
        return left

    def _parse_and(self) -> Any:
        left = self._parse_not()
        while (tok := self._peek()) and tok.kind == _TokKind.KEYWORD and tok.value == "and":
            self._advance()
            left = _And(left, self._parse_not())
        return left

    def _parse_not(self) -> Any:
        if (tok := self._peek()) and tok.kind == _TokKind.KEYWORD and tok.value == "not":
            self._advance()
            return _Not(self._parse_not())
        return self._parse_comparison()

    def _parse_comparison(self) -> Any:
        left = self._parse_primary()
        if (tok := self._peek()) and tok.kind == _TokKind.OP:
            op = self._advance().value
            right = self._parse_primary()
            return _Compare(op, left, right)
        return left

    def _parse_primary(self) -> Any:  # noqa: PLR0911,PLR0912
        tok = self._peek()
        if tok is None:
            raise _SyntaxError_(f"position {self.pos}: 表达式意外结束")
        if tok.kind == _TokKind.BOOL:
            self._advance()
            return _Literal({"true": True, "false": False, "none": None}[tok.value])
        if tok.kind == _TokKind.NUMBER:
            self._advance()
            num = float(tok.value)
            return _Literal(int(num) if num.is_integer() and "." not in tok.value else num)
        if tok.kind == _TokKind.STRING:
            self._advance()
            return _Literal(tok.value)
        if tok.kind == _TokKind.LBRACKET:
            return self._parse_list()
        if tok.kind == _TokKind.LPAREN:
            self._advance()
            inner = self._parse_or()
            close = self._advance()
            if close.kind != _TokKind.RPAREN:
                raise _SyntaxError_(f"position {self.pos}: 期望 ')'")
            return inner
        if tok.kind == _TokKind.IDENT:
            self._advance()
            steps: list[tuple[str, Any]] = []
            while (nxt := self._peek()) is not None:
                if nxt.kind == _TokKind.DOT:
                    self._advance()
                    field_tok = self._advance()
                    if field_tok.kind != _TokKind.IDENT:
                        raise _SyntaxError_(f"position {self.pos}: '.' 后应为字段名（不支持方法调用）")
                    steps.append(("field", field_tok.value))
                elif nxt.kind == _TokKind.LBRACKET:
                    self._advance()
                    key = self._parse_primary()
                    close = self._advance()
                    if close.kind != _TokKind.RBRACKET:
                        raise _SyntaxError_(f"position {self.pos}: 期望 ']'")
                    steps.append(("index", key))
                else:
                    break
            return _Path(tok.value, steps)
        raise _SyntaxError_(f"position {self.pos}: 无法识别的 token '{tok.value}'")

    def _parse_list(self) -> Any:
        self._advance()  # 消耗 [
        items: list[Any] = []
        if (tok := self._peek()) and tok.kind == _TokKind.RBRACKET:
            self._advance()
            return _List(items)
        while True:
            items.append(self._parse_primary())
            nxt = self._peek()
            if nxt is None:
                raise _SyntaxError_(f"position {self.pos}: 列表未闭合")
            if nxt.kind == _TokKind.COMMA:
                self._advance()
                continue
            if nxt.kind == _TokKind.RBRACKET:
                self._advance()
                return _List(items)
            raise _SyntaxError_(f"position {self.pos}: 列表内意外的 token '{nxt.value}'（期望 ',' 或 ']'）")


# ── 求值 ─────────────────────────────────────────────────────


def _truthy(value: Any) -> bool:
    """Python bool() 语义（None/0/''/[]/{} 为假）。"""
    return bool(value)


def _eval_value(expr: Any, context: dict[str, Any]) -> Any:  # noqa: PLR0911
    if isinstance(expr, _Literal):
        return expr.value
    if isinstance(expr, _List):
        return [_eval_value(item, context) for item in expr.items]
    if isinstance(expr, _Path):
        return expr.resolve(context)
    if isinstance(expr, _Not):
        return not _truthy(_eval_value(expr.operand, context))
    if isinstance(expr, _And):
        if not _truthy(_eval_value(expr.left, context)):
            return False
        return _truthy(_eval_value(expr.right, context))
    if isinstance(expr, _Or):
        if _truthy(_eval_value(expr.left, context)):
            return True
        return _truthy(_eval_value(expr.right, context))
    if isinstance(expr, _Compare):
        left = _eval_value(expr.left, context)
        right = _eval_value(expr.right, context)
        return _compare(left, expr.op, right)
    return None


def _compare(left: Any, op: str, right: Any) -> bool:  # noqa: PLR0912
    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right  # type: ignore[operator]
        if op == "<":
            return left < right  # type: ignore[operator]
        if op == ">=":
            return left >= right  # type: ignore[operator]
        if op == "<=":
            return left <= right  # type: ignore[operator]
    except TypeError:
        # 类型不可比较（如 str > int）→ False，与安全兜底一致
        return False
    return False


def compile_condition(expression: str) -> Any | None:
    """把表达式编译为 AST（None = 空表达式恒真）。

    Raises:
        _SyntaxError_: 语法错误——注册期可捕获提前暴露。
    """
    text = expression.strip()
    if not text:
        return None
    return _Parser(_tokenize(text)).parse()


def eval_compiled(ast: Any, context: dict[str, Any]) -> bool:
    """对已编译 AST 求值（运行时零解析）。恒真（None）由调用方短路。"""
    return _truthy(_eval_value(ast, context))


def parse_condition(expression: str, context: dict[str, Any]) -> bool:
    """安全求值条件表达式（兼容入口，0.1 同签名）。

    语法/求值异常返回 False（安全兜底，不向调用方抛出）。
    """
    try:
        ast = compile_condition(expression)
    except _SyntaxError_:
        return False
    except Exception:
        return False
    if ast is None:
        return True
    try:
        return eval_compiled(ast, context)
    except Exception:
        return False
