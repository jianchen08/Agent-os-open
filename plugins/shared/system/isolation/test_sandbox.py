# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation sandbox.py 代码沙箱测试（A5.3 补）。

覆盖：
1. CodeValidator 白名单校验（import/未列内置/dunder/属性访问拒绝,方法调用放行）；
2. SandboxConfig 校验（负超时/负内存拒绝）；
3. CodeSandbox.execute 全分支（成功/返回值/超时/语法错/运行错/安全拒绝/上下文/统计）；
4. call_function / reset / get_stats。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_sandbox_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "sandbox.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
CodeSandbox = _MOD.CodeSandbox
CodeValidator = _MOD.CodeValidator
SandboxConfig = _MOD.SandboxConfig
SandboxResult = _MOD.SandboxResult
ALLOWED_BUILTINS = _MOD.ALLOWED_BUILTINS


def _run(coro) -> Any:
    # 共享测试进程中其他测试可能关闭主 loop,须自建独立 loop
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestValidatorAllow:
    def test_plain_expression(self) -> None:
        ok, issues = CodeValidator(SandboxConfig()).validate("1 + 2")
        assert ok and issues == []

    def test_allowed_builtins_calls(self) -> None:
        ok, _ = CodeValidator(SandboxConfig()).validate("sum([1,2,3]) + len('abc') + max(1, 5)")
        assert ok

    def test_method_call_allowed(self) -> None:
        ok, _ = CodeValidator(SandboxConfig()).validate("'a,b'.split(',')")
        assert ok

    def test_function_definition_allowed(self) -> None:
        ok, _ = CodeValidator(SandboxConfig()).validate("def f(x): return x * 2")
        assert ok


class TestValidatorRejects:
    def test_import_rejected(self) -> None:
        ok, issues = CodeValidator(SandboxConfig()).validate("import os")
        assert not ok
        assert any("import" in i for i in issues)

    def test_unlisted_builtin_rejected(self) -> None:
        ok, issues = CodeValidator(SandboxConfig()).validate("print('hi')")
        assert not ok
        assert any("print" in i for i in issues)

    def test_dunder_method_call_rejected(self) -> None:
        ok, issues = CodeValidator(SandboxConfig()).validate("x.__class__()")
        assert not ok
        assert any("__class__" in i for i in issues)

    def test_attribute_access_rejected(self) -> None:
        ok, issues = CodeValidator(SandboxConfig()).validate("x.real")
        assert not ok
        assert any("属性访问" in i for i in issues)

    def test_syntax_error_passes_to_execute(self) -> None:
        # 语法错误不是安全问题 → 放行,由执行阶段报 SyntaxError
        ok, issues = CodeValidator(SandboxConfig()).validate("def (")
        assert ok and issues == []


class TestSandboxConfig:
    def test_negative_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="超时"):
            SandboxConfig(timeout_seconds=-1)

    def test_negative_memory_rejected(self) -> None:
        with pytest.raises(ValueError, match="内存"):
            SandboxConfig(max_memory=-1)

    def test_default_lists_are_independent(self) -> None:
        c1, c2 = SandboxConfig(), SandboxConfig()
        c1.allowed_modules.append("x")
        assert "x" not in c2.allowed_modules


class TestExecuteSuccess:
    def test_basic_execution_output(self) -> None:
        result = _run(CodeSandbox().execute("x = sum([1, 2, 3])"))
        assert result.success
        assert result.output == ""
        assert result.error is None

    def test_return_var_from_locals(self) -> None:
        result = _run(CodeSandbox().execute("x = 42", return_var="x"))
        assert result.success
        assert result.return_value == 42

    def test_return_var_from_globals(self) -> None:
        """__ 前缀变量不进 exec_locals,只留在 exec_globals 时从全局取返回值。"""
        result = _run(CodeSandbox().execute("__x = 5", return_var="__x"))
        assert result.success
        assert result.return_value == 5

    def test_return_var_absent_returns_none(self) -> None:
        result = _run(CodeSandbox().execute("y = 1", return_var="missing"))
        assert result.success
        assert result.return_value is None

    def test_context_variables_available(self) -> None:
        result = _run(CodeSandbox().execute("total = base + 1", context={"base": 10}, return_var="total"))
        assert result.success
        assert result.return_value == 11

    def test_sandbox_result_to_dict(self) -> None:
        d = SandboxResult(success=True, output="o", return_value=1).to_dict()
        assert d["success"] is True
        assert d["return_value"] == 1


class TestExecuteFailures:
    def test_security_rejection(self) -> None:
        result = _run(CodeSandbox().execute("import os"))
        assert not result.success
        assert result.error_type == "SecurityError"
        assert "import" in (result.error or "")

    def test_syntax_error(self) -> None:
        result = _run(CodeSandbox().execute("def ("))
        assert not result.success
        assert result.error_type == "SyntaxError"

    def test_runtime_error(self) -> None:
        result = _run(CodeSandbox().execute("1 / 0"))
        assert not result.success
        assert result.error_type == "ZeroDivisionError"
        assert result.output == ""  # stderr 被捕获,output 不含错误文本

    def test_timeout(self) -> None:
        sandbox = CodeSandbox(SandboxConfig(timeout_seconds=0.05))
        # time 模块由宿主预载(allowed_modules),无需 import;sleep 阻塞触发超时
        result = _run(sandbox.execute("time.sleep(0.5)"))
        assert not result.success
        assert result.error_type == "TimeoutError"
        assert "超时" in (result.error or "")

    def test_unlisted_builtin_runtime_fails(self) -> None:
        """属性方法形态绕过 AST 校验(方法调用放行),运行时未定义名 → NameError。"""
        result = _run(CodeSandbox().execute("(1).nope()"))
        assert not result.success
        assert result.error_type == "AttributeError"


class TestStatsAndReset:
    def test_stats_counts(self) -> None:
        sandbox = CodeSandbox()
        _run(sandbox.execute("x = 1"))
        _run(sandbox.execute("import os"))
        _run(sandbox.execute("1 / 0"))
        stats = sandbox.get_stats()
        assert stats["total_executions"] == 3
        assert stats["successful_executions"] == 1
        assert stats["failed_executions"] == 2

    def test_reset_clears_state(self) -> None:
        sandbox = CodeSandbox()
        _run(sandbox.execute("def f(): return 1"))
        _run(sandbox.call_function("f"))
        _run(sandbox.reset())
        # 重置后函数丢失
        result = _run(sandbox.call_function("f"))
        assert not result.success
        assert result.error_type == "NameError"


class TestCallFunction:
    def test_call_existing_function(self) -> None:
        sandbox = CodeSandbox()
        _run(sandbox.execute("def add(a, b): return a + b"))
        result = _run(sandbox.call_function("add", args=[2, 3]))
        assert result.success
        assert result.return_value == 5

    def test_call_with_kwargs(self) -> None:
        sandbox = CodeSandbox()
        _run(sandbox.execute("def double(x): return x * 2"))
        result = _run(sandbox.call_function("double", kwargs={"x": 4}))
        assert result.success
        assert result.return_value == 8

    def test_call_missing_function(self) -> None:
        result = _run(CodeSandbox().call_function("nope"))
        assert not result.success
        assert result.error_type == "NameError"

    def test_call_function_raises(self) -> None:
        sandbox = CodeSandbox()
        _run(sandbox.execute("def boom(): return 1 / 0"))
        result = _run(sandbox.call_function("boom"))
        assert not result.success
        assert result.error_type == "ZeroDivisionError"
