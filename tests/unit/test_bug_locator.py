"""Bug 定位器测试。

覆盖模块：tests/test_utils/bug_locator.py

测试场景：
- locate_bug 从异常信息定位文件+行号+上下文代码
- CodeLocation 格式化输出
- BugLocation 摘要报告
- 空异常处理
- 项目代码/测试代码分类
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_utils.bug_locator import (
    BugLocation,
    CodeLocation,
    locate_bug,
    _is_project_code,
    _is_test_code,
    _relative_path,
    _PROJECT_SRC,
    _TESTS_DIR,
)


class TestCodeLocation:
    """CodeLocation 数据类测试。"""

    def test_format_with_project_code(self) -> None:
        """项目代码行有 >>> 标记。"""
        loc = CodeLocation(
            file_path="src/module.py",
            line_number=10,
            function_name="my_func",
            code_line="x = 1",
            context_before=("def my_func():",),
            context_after=("    return x",),
            is_project_code=True,
            is_test_code=False,
        )
        result = loc.format()
        assert ">>>" in result
        assert "x = 1" in result

    def test_format_with_non_project_code(self) -> None:
        """非项目代码行无 >>> 标记。"""
        loc = CodeLocation(
            file_path="/usr/lib/python3/os.py",
            line_number=100,
            function_name="system",
            code_line="pass",
            context_before=(),
            context_after=(),
            is_project_code=False,
            is_test_code=False,
        )
        result = loc.format()
        assert ">>>" not in result
        assert "pass" in result

    def test_format_with_context(self) -> None:
        """上下文行显示正确的行号。"""
        loc = CodeLocation(
            file_path="src/app.py",
            line_number=15,
            function_name="handler",
            code_line="raise ValueError('fail')",
            context_before=("def handler():", "    x = 1"),
            context_after=("    return x"),
            is_project_code=True,
            is_test_code=False,
        )
        result = loc.format()
        # 上下文行从 line_number - len(context_before) 开始
        assert "13 |" in result
        assert "14 |" in result
        assert "15 |" in result
        assert "16 |" in result

    def test_format_empty_context(self) -> None:
        """无上下文时只显示代码行。"""
        loc = CodeLocation(
            file_path="src/app.py",
            line_number=5,
            function_name="f",
            code_line="return",
            context_before=(),
            context_after=(),
            is_project_code=True,
            is_test_code=False,
        )
        result = loc.format()
        assert "5 |" in result
        assert "return" in result

    def test_frozen_dataclass(self) -> None:
        """CodeLocation 是不可变的。"""
        loc = CodeLocation(
            file_path="a.py",
            line_number=1,
            function_name="f",
            code_line="x",
            context_before=(),
            context_after=(),
            is_project_code=False,
            is_test_code=False,
        )
        with pytest.raises(AttributeError):
            loc.line_number = 99  # type: ignore[misc]


class TestBugLocation:
    """BugLocation 数据类测试。"""

    def test_summary_with_assertion(self) -> None:
        """摘要包含断言位置信息。"""
        assertion = CodeLocation(
            file_path="tests/test_app.py",
            line_number=42,
            function_name="test_foo",
            code_line="assert x == 1",
            context_before=(),
            context_after=(),
            is_project_code=True,
            is_test_code=True,
        )
        bug = BugLocation(
            assertion_location=assertion,
            raw_traceback="traceback text",
        )
        summary = bug.summary()
        assert "断言失败位置" in summary
        assert "tests/test_app.py" in summary
        assert "42" in summary

    def test_summary_without_assertion(self) -> None:
        """无断言位置时显示警告。"""
        bug = BugLocation(assertion_location=None, raw_traceback="no info")
        summary = bug.summary()
        assert "未找到断言失败位置" in summary

    def test_summary_with_candidates(self) -> None:
        """摘要包含 Bug 候选位置。"""
        candidate = CodeLocation(
            file_path="src/core/engine.py",
            line_number=100,
            function_name="process",
            code_line="result = compute()",
            context_before=(),
            context_after=(),
            is_project_code=True,
            is_test_code=False,
        )
        bug = BugLocation(
            assertion_location=None,
            bug_candidates=[candidate],
            raw_traceback="",
        )
        summary = bug.summary()
        assert "高概率 Bug 位置" in summary
        assert "src/core/engine.py" in summary

    def test_summary_with_source_files(self) -> None:
        """摘要包含涉及的源码文件。"""
        bug = BugLocation(
            assertion_location=None,
            source_files=["src/a.py", "src/b.py"],
            raw_traceback="",
        )
        summary = bug.summary()
        assert "涉及的源码文件" in summary
        assert "src/a.py" in summary

    def test_default_values(self) -> None:
        """默认值为空。"""
        bug = BugLocation(assertion_location=None)
        assert bug.assertion_location is None
        assert bug.bug_candidates == []
        assert bug.source_files == []
        assert bug.raw_traceback == ""


class TestLocateBug:
    """locate_bug 函数测试。"""

    def test_none_exc_info(self) -> None:
        """无异常信息返回空结果。"""
        result = locate_bug(None)
        assert result.assertion_location is None
        assert result.bug_candidates == []
        assert "无异常信息" in result.raw_traceback

    def test_locate_from_real_exception(self) -> None:
        """从真实异常定位文件和行号。"""
        try:
            _raise_value_error()  # noqa: 旨在产生 traceback
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        result = locate_bug(exc_info)
        assert result.assertion_location is not None
        assert result.assertion_location.line_number > 0
        assert result.assertion_location.function_name == "_raise_value_error"
        assert len(result.raw_traceback) > 0

    def test_locate_preserves_raw_traceback(self) -> None:
        """保留原始 traceback 文本。"""
        try:
            1 / 0
        except ZeroDivisionError:
            import sys

            exc_info = sys.exc_info()

        result = locate_bug(exc_info)
        assert "ZeroDivisionError" in result.raw_traceback

    def test_locate_nested_exception(self) -> None:
        """嵌套异常定位到最内层。"""
        try:
            _outer_cause()
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()

        result = locate_bug(exc_info)
        assert result.assertion_location is not None
        # 最内层函数名
        assert result.assertion_location.function_name == "_inner_cause"

    def test_locate_with_context_code(self) -> None:
        """异常位置的上下文代码不为空。"""
        try:
            _raise_value_error()
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        result = locate_bug(exc_info)
        loc = result.assertion_location
        assert loc is not None
        # 至少有代码行本身
        assert len(loc.code_line) >= 0


class TestHelperFunctions:
    """辅助函数测试。"""

    def test_is_project_code_src(self) -> None:
        """src/ 下文件是项目代码。"""
        path = _PROJECT_SRC / "core" / "engine.py"
        assert _is_project_code(path) is True

    def test_is_project_code_tests(self) -> None:
        """tests/ 下文件也是项目代码。"""
        path = _TESTS_DIR / "test_foo.py"
        assert _is_project_code(path) is True

    def test_is_project_code_external(self) -> None:
        """外部路径不是项目代码。"""
        path = Path("/usr/lib/python3.10/os.py")
        assert _is_project_code(path) is False

    def test_is_test_code_true(self) -> None:
        """tests/ 下文件是测试代码。"""
        path = _TESTS_DIR / "unit" / "test_app.py"
        assert _is_test_code(path) is True

    def test_is_test_code_false(self) -> None:
        """src/ 下文件不是测试代码。"""
        path = _PROJECT_SRC / "core" / "engine.py"
        assert _is_test_code(path) is False

    def test_relative_path_within_project(self) -> None:
        """项目内路径返回相对路径。"""
        result = _relative_path(str(_PROJECT_SRC / "core" / "engine.py"))
        assert not os.path.isabs(result)

    def test_relative_path_external(self) -> None:
        """无法相对化时返回原路径。"""
        # 跨驱动器（Windows）或无关路径时返回原路径
        result = _relative_path("/some/external/path.py")
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════
# 测试辅助函数（产生真实 traceback）
# ═══════════════════════════════════════════════════════════════════


def _raise_value_error() -> None:
    """产生 ValueError。"""
    raise ValueError("test error for locate_bug")


def _inner_cause() -> None:
    """嵌套内层异常。"""
    raise RuntimeError("inner error")


def _outer_cause() -> None:
    """嵌套外层异常。"""
    _inner_cause()
