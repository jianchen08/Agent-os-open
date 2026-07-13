"""M5 安全回归：sandbox CodeValidator 拦截 dunder 属性链逃逸。

漏洞：core/sandbox.py 的 CodeValidator.validate 只查 Import/ImportFrom/Call，
不覆盖 Attribute 链逃逸。().__class__.__bases__[0].__subclasses__() 这类
payload 不经过上述节点，直接绕过到 object/os，在宿主进程内获得完整权限。

修复：AST 检查新增 dunder 属性黑名单（_DUNDER_ESCAPE_ATTRS），拦截
__class__/__bases__/__subclasses__/__globals__/__builtins__ 等已知跳板。

注意：这是黑名单性质，挡公开 payload；根治需容器隔离（见 sandbox.py
模块文档警告）。本测试守护"黑名单存在且生效"这个底线。

同时守护既有检查（os/system/import）不被本次改动破坏。
"""
from __future__ import annotations

import pytest

from src.core.sandbox import CodeValidator, SandboxConfig


@pytest.fixture
def validator() -> CodeValidator:
    return CodeValidator(SandboxConfig())


class TestDunderEscapeBlocked:
    """M5: dunder 属性链逃逸 payload 必须被拦截。"""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("x = ().__class__.__bases__[0].__subclasses__()", id="subclass_chain"),
            pytest.param("y = ().__class__.__mro__", id="mro_walk"),
            pytest.param("g = print.__globals__", id="func_globals"),
            pytest.param("b = print.__builtins__", id="func_builtins"),
            pytest.param("c = ().__class__", id="bare_class_access"),
        ],
    )
    def test_dunder_escape_blocked(self, validator: CodeValidator, payload: str) -> None:
        ok, issues = validator.validate(payload)
        assert not ok, f"dunder 逃逸未被拦截: {payload} -> issues={issues}"
        # 错误信息应提及 dunder
        assert any("dunder" in i or "__" in i for i in issues), issues


class TestNormalCodeStillPasses:
    """M5: 正常代码不应被 dunder 黑名单误伤。"""

    def test_math_and_comprehension(self, validator: CodeValidator) -> None:
        ok, issues = validator.validate(
            "import math\nresult = [math.sqrt(i) for i in range(10)]\nprint(result)"
        )
        assert ok, f"正常代码被误拦: {issues}"

    def test_string_operations(self, validator: CodeValidator) -> None:
        ok, issues = validator.validate(
            "names = ['a', 'b', 'c']\njoined = '-'.join(names)\nprint(joined)"
        )
        assert ok, f"字符串操作被误拦: {issues}"


class TestExistingChecksIntact:
    """M5: 既有检查（os/import/blocked builtins）不能被本次改动破坏。"""

    def test_os_import_blocked(self, validator: CodeValidator) -> None:
        ok, _ = validator.validate("import os\nos.system('id')")
        assert not ok

    def test_exec_blocked(self, validator: CodeValidator) -> None:
        ok, _ = validator.validate("exec(\"import os\")")
        assert not ok
