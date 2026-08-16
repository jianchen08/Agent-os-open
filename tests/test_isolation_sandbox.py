# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""isolation sandbox 定位回归测试（F-SANDBOX-2：白名单模式）。

意图：sandbox 模块是**轻量代码沙箱**，只面向已审批 HOST 模式下的受信代码，
**不是安全边界**；不可信代码必须走容器隔离（DockerProvider）。F-SANDBOX-2
把范式从「黑名单拒绝」翻转为「白名单放行（fail-closed）」：只允许明确列出的
安全操作，一切未列出项默认拒绝。本测试锁定：

1. 经典类对象逃逸 `().__class__.__mro__[-1].__subclasses__()` 被拒绝（审计
   指出该链可拿到任意类引用进而逃逸获取 os）；
2. getattr / type / dir / globals / eval / import / __class__ 链等一律拒绝；
3. **默认拒绝（fail-closed）**：黑名单之外但未列入白名单的内置
   （ord/chr/bin/hex/print/hash/id 等）与一切未知调用一律 SecurityError；
4. 白名单内置（abs/len/sum/sorted/str/int/list/range/enumerate 等）可用，
   常规算术 / 字符串 / 容器 / 循环不回归；
5. 模块 docstring 明确声明「白名单模式 / 非安全边界 / 仅受信代码」，
   防止后续维护者误用。
"""
from __future__ import annotations

from typing import Any

import pytest

import tests._isolation_path  # noqa: F401  (把 isolation 插件目录加入 sys.path；须在 sandbox import 前执行)
from sandbox import CodeSandbox, SandboxConfig


@pytest.fixture
def sandbox() -> CodeSandbox:
    """每个用例独立沙箱，避免用例间共享可变状态（testing_rules §4.1）。"""
    return CodeSandbox(SandboxConfig())


async def test_escape_via_class_walk_rejected(sandbox: CodeSandbox) -> None:
    """意图：`().__class__.__mro__[-1].__subclasses__()` 是经典 Python 沙箱逃逸，
    返回所有类的引用，攻击者遍历后即可拿到 os 等敏感能力。沙箱必须拒绝而非放行。"""
    result = await sandbox.execute("().__class__.__mro__[-1].__subclasses__()")
    assert result.success is False
    assert result.error_type == "SecurityError"


async def test_escape_via_getattr_rejected(sandbox: CodeSandbox) -> None:
    """意图：getattr 是审计点名的可逃逸原语，`getattr(x, '__class__')` 与属性访问
    等价，可绕过任何「字面量属性」类检查；白名单模式未列出 getattr，必须拒绝。"""
    result = await sandbox.execute("getattr((), '__class__')")
    assert result.success is False
    assert result.error_type == "SecurityError"


@pytest.mark.parametrize(
    "code",
    ["type(1)", "dir(())", "globals()", "vars()", "object()", "locals()"],
)
async def test_escapable_builtins_removed_from_runtime(sandbox: CodeSandbox, code: str) -> None:
    """意图：type/dir/globals/vars/object/locals 均可用于对象自省逃逸链
    （如 type(x).__mro__ / vars 翻命名空间），白名单模式下执行必须失败。"""
    result = await sandbox.execute(code)
    assert result.success is False


async def test_basic_arithmetic_still_works(sandbox: CodeSandbox) -> None:
    """意图：收紧只移除自省/逃逸原语，算术等基本能力必须保持可用。"""
    result = await sandbox.execute("answer = 1 + 2 * 3", return_var="answer")
    assert result.success is True
    assert result.return_value == 7


async def test_basic_string_and_container_ops_still_work(sandbox: CodeSandbox) -> None:
    """意图：字符串方法与容器字面量是宿主代码基本能力，不得受收紧影响。"""
    result = await sandbox.execute(
        "text = 'ab' + 'cd'; parts = text.split('b'); total = sum([1, 2, 3])",
        return_var="total",
    )
    assert result.success is True
    assert result.return_value == 6


# ── F-SANDBOX-2：白名单模式 ─────────────────────────────────────────────

# 白名单内置的可验证用例（AST 白名单 + safe_builtins 双保险：任一缺失即红）
WHITELISTED_CALL_CASES: list[tuple[str, Any]] = [
    ("abs(-5)", 5),
    ("len('abc')", 3),
    ("min([3, 1, 2])", 1),
    ("max(1, 2, 3)", 3),
    ("round(1.234, 2)", 1.23),
    ("str(42)", "42"),
    ("int('7')", 7),
    ("float('1.5')", 1.5),
    ("bool(0)", False),
    ("list('ab')", ["a", "b"]),
    ("dict(a=1)", {"a": 1}),
    ("set([1, 2, 2])", {1, 2}),
    ("tuple([1, 2])", (1, 2)),
    ("sorted([3, 1, 2])", [1, 2, 3]),
    ("format(1.5, '.1f')", "1.5"),
    ("repr(1)", "1"),
    ("any([False, True])", True),
    ("all([1, 2])", True),
    ("list(range(4))", [0, 1, 2, 3]),
    ("list(enumerate('ab'))", [(0, "a"), (1, "b")]),
    ("list(zip([1, 2], [3, 4]))", [(1, 3), (2, 4)]),
    ("list(reversed([1, 2, 3]))", [3, 2, 1]),
    ("list(map(str, [1, 2]))", ["1", "2"]),
    ("list(filter(None, [0, 1]))", [1]),
    ("pow(2, 10)", 1024),
    ("divmod(7, 2)", (3, 1)),
    ("isinstance('a', str)", True),
    ("next(iter([1, 2]))", 1),
]


@pytest.mark.parametrize("code,expected", WHITELISTED_CALL_CASES)
async def test_whitelisted_builtins_available(
    sandbox: CodeSandbox, code: str, expected: Any
) -> None:
    """意图：白名单内置是日常计算/字符串/容器的安全默认集，必须可用且结果正确；
    任一白名单缺失（AST 拒绝或运行时 NameError）都会让本用例红。"""
    result = await sandbox.execute(f"r = {code}", return_var="r")
    assert result.success is True, result.error
    assert result.return_value == expected


# 默认拒绝（fail-closed）：旧黑名单模式会放行 ord/chr/bin/hex/print/hash/id 等
REJECTED_CALL_CASES: list[str] = [
    # 黑名单之外但未列入白名单 → 默认拒绝
    "ord('a')",
    "chr(97)",
    "bin(5)",
    "hex(255)",
    "print('x')",
    "hash(1)",
    "id(1)",
    # 反射 / IO / 执行原语（白名单模式天然拒绝）
    "eval('1')",
    "exec('1')",
    "compile('1', '<s>', 'exec')",
    "open('f.txt')",
    "input()",
    "getattr((), '__class__')",
    "setattr((), 'x', 1)",
    "delattr((), 'x')",
    "globals()",
    "locals()",
    "vars()",
    "type(1)",
    "dir(())",
    "object()",
    "super()",
    "memoryview(b'x')",
    "hasattr((), 'x')",
    "breakpoint()",
    "exit()",
    "quit()",
    "__import__('os')",
]


@pytest.mark.parametrize("code", REJECTED_CALL_CASES)
async def test_non_whitelisted_calls_rejected(sandbox: CodeSandbox, code: str) -> None:
    """意图：白名单模式 fail-closed——一切未列入白名单的调用一律在 AST 层拒绝为
    SecurityError，包括旧黑名单漏掉的 ord/chr/bin/hex/print/hash/id 等纯函数。"""
    result = await sandbox.execute(code)
    assert result.success is False
    assert result.error_type == "SecurityError"


async def test_unknown_callable_rejected_at_ast(sandbox: CodeSandbox) -> None:
    """意图：未列入白名单的名字调用（即使运行时可能不存在）也在验证阶段即拒绝，
    证明 fail-closed 生效于 AST 层而非等到运行时 NameError。"""
    result = await sandbox.execute("foo(1)")
    assert result.success is False
    assert result.error_type == "SecurityError"


@pytest.mark.parametrize("code", ["import math", "from math import sqrt", "import os"])
async def test_import_statements_rejected(sandbox: CodeSandbox, code: str) -> None:
    """意图：白名单模式下一律禁止 import 语句（含 allowed_modules 内的 math）——
    模块只能由宿主预载，代码侧不可自行导入，防止绕过受限命名空间。"""
    result = await sandbox.execute(code)
    assert result.success is False
    assert result.error_type == "SecurityError"


async def test_attribute_value_access_rejected(sandbox: CodeSandbox) -> None:
    """意图：非调用形式的属性取值（如 x.real、[].count）白名单模式一律拒绝
    （fail-closed）；旧黑名单模式会放行，必须翻转为拒绝。"""
    for code in ("x = 5; y = x.real", "[1, 2, 3].count"):
        result = await sandbox.execute(code)
        assert result.success is False, code
        assert result.error_type == "SecurityError", code


async def test_dunder_method_call_rejected(sandbox: CodeSandbox) -> None:
    """意图：下划线开头的方法调用（[].__len__()、().__subclasses__()）属 dunder
    逃逸面，白名单只放行普通方法，dunder 方法一律拒绝。"""
    for code in ("[].__len__()", "().__subclasses__()"):
        result = await sandbox.execute(code)
        assert result.success is False, code
        assert result.error_type == "SecurityError", code


async def test_method_calls_on_values_still_work(sandbox: CodeSandbox) -> None:
    """意图：字符串/容器的方法调用（split/join）是宿主代码基本能力，白名单只拒绝
    下划线开头的 dunder 方法，普通方法调用不得回归。"""
    result = await sandbox.execute(
        "parts = 'a,b,c'.split(','); joined = '-'.join(parts); total = len(joined)",
        return_var="total",
    )
    assert result.success is True
    assert result.return_value == 5


async def test_loops_and_comprehensions_still_work(sandbox: CodeSandbox) -> None:
    """意图：常规 for 循环与列表推导依赖 range 与基础语法，白名单模式下不得回归。"""
    result = await sandbox.execute(
        "total = 0\n"
        "for i in range(5):\n"
        "    total += i\n"
        "squares = [x * x for x in range(4)]",
        return_var="squares",
    )
    assert result.success is True
    assert result.return_value == [0, 1, 4, 9]


def test_module_docstring_declares_non_security_boundary() -> None:
    """意图：文档化是「安全默认」的第一步——模块必须显式声明自己不是安全边界、
    仅用于已审批 HOST 模式的受信代码，且已切换为白名单模式（默认拒绝一切
    未列出项），否则后续维护者会误当作隔离机制使用。"""
    import sandbox

    doc = sandbox.__doc__ or ""
    assert "白名单" in doc
    assert "非安全边界" in doc
    assert "受信代码" in doc
