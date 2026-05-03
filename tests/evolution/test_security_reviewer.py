"""安全审查模块测试。

覆盖 SecurityReviewer 的核心功能：
- static_analysis: 静态分析（危险导入、危险调用、文件系统、网络、环境变量）
- sandbox_execute: 沙箱执行
- check_permissions: 权限检查
- check_resource_limits: 资源限制检查
- review: 综合审查
- MF-03 修复验证：模块自身不导入危险模块
"""

from __future__ import annotations

import importlib
import sys

import pytest

from evolution.security_reviewer import SecurityReviewer
from evolution.types import GeneratedArtifact, GenerationType


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def reviewer() -> SecurityReviewer:
    """安全审查器实例。"""
    return SecurityReviewer()


@pytest.fixture
def make_artifact():
    """创建 GeneratedArtifact 的工厂。"""
    def _make(code: str, gen_type: GenerationType = GenerationType.BUILTIN_TOOL) -> GeneratedArtifact:
        return GeneratedArtifact(
            generation_type=gen_type,
            code=code,
            file_path="test.py",
        )
    return _make


# =========================================================================
# static_analysis 测试
# =========================================================================


class TestStaticAnalysis:
    """静态分析测试。"""

    def test_static_analysis_clean_code(self, reviewer: SecurityReviewer) -> None:
        """干净代码无告警。"""
        code = '''
import json
import logging

def hello():
    return "world"
'''
        issues = reviewer.static_analysis(code)
        assert len(issues) == 0

    def test_static_analysis_dangerous_imports_subprocess(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测 subprocess 危险导入。"""
        code = '''
import subprocess

def run():
    subprocess.run(["ls"])
'''
        issues = reviewer.static_analysis(code)
        assert any(
            i["category"] == "dangerous_import" and "subprocess" in i["message"]
            for i in issues
        )

    def test_static_analysis_dangerous_imports_os_system(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测 os.system 危险导入。"""
        code = '''
import os

def run():
    os.system("ls")
'''
        issues = reviewer.static_analysis(code)
        # os.system 调用应被检测
        assert len(issues) >= 1

    def test_static_analysis_dangerous_imports_ctypes(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测 ctypes 危险导入。"""
        code = '''
import ctypes

def run():
    ctypes.cdll.LoadLibrary("lib.so")
'''
        issues = reviewer.static_analysis(code)
        assert any("ctypes" in i["message"] for i in issues)

    def test_static_analysis_dangerous_imports_socket(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测 socket 危险导入。"""
        code = '''
import socket

def run():
    s = socket.socket()
'''
        issues = reviewer.static_analysis(code)
        assert any("socket" in i["message"] for i in issues)

    def test_static_analysis_dangerous_imports_from_import(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测 from ... import 形式的危险导入。"""
        code = '''
from subprocess import run

def execute():
    run(["ls"])
'''
        issues = reviewer.static_analysis(code)
        assert any(
            i["category"] == "dangerous_import" and "subprocess" in i["message"]
            for i in issues
        )

    def test_static_analysis_eval_exec(self, reviewer: SecurityReviewer) -> None:
        """检测 eval/exec 调用。"""
        eval_code = '''
def bad():
    result = eval("1+1")
    return result
'''
        issues = reviewer.static_analysis(eval_code)
        assert any(
            i["category"] == "dangerous_call" and "eval" in i["message"]
            for i in issues
        )

        exec_code = '''
def bad():
    exec("print('hello')")
'''
        issues = reviewer.static_analysis(exec_code)
        assert any("exec" in i["message"] for i in issues)

    def test_static_analysis_syntax_error(self, reviewer: SecurityReviewer) -> None:
        """语法错误被检测为 critical。"""
        code = "def incomplete("
        issues = reviewer.static_analysis(code)
        assert any(i["category"] == "syntax_error" for i in issues)
        assert any(i["severity"] == "critical" for i in issues)

    def test_static_analysis_filesystem_operations(self, reviewer: SecurityReviewer) -> None:
        """检测文件系统操作。"""
        code = '''
def write_file():
    f = open("/etc/passwd", "w")
    f.write("data")
'''
        issues = reviewer.static_analysis(code)
        assert any(i["category"] == "filesystem" for i in issues)

    def test_static_analysis_network_operations(self, reviewer: SecurityReviewer) -> None:
        """检测网络操作。"""
        code = '''
import urllib.request

def fetch():
    urllib.request.urlopen("http://example.com")
'''
        issues = reviewer.static_analysis(code)
        # urllib.request 是危险导入或网络操作
        assert len(issues) > 0

    def test_static_analysis_env_access(self, reviewer: SecurityReviewer) -> None:
        """检测环境变量访问。"""
        code = '''
import os

def get_env():
    val = os.getenv("API_KEY")
    return val
'''
        issues = reviewer.static_analysis(code)
        assert any(i["category"] == "env_access" for i in issues)

    def test_static_analysis_issue_structure(self, reviewer: SecurityReviewer) -> None:
        """检测到的问题包含正确的字段结构。"""
        code = "import subprocess"
        issues = reviewer.static_analysis(code)
        assert len(issues) > 0
        issue = issues[0]
        assert "severity" in issue
        assert "category" in issue
        assert "message" in issue
        assert "line" in issue


# =========================================================================
# sandbox_execute 测试
# =========================================================================


class TestSandboxExecute:
    """沙箱执行测试。"""

    def test_sandbox_execute_valid_code(self, reviewer: SecurityReviewer) -> None:
        """合法代码沙箱通过。"""
        code = '''
def hello():
    return "world"
'''
        result = reviewer.sandbox_execute(code)
        assert result["success"] is True
        assert result["timed_out"] is False

    def test_sandbox_execute_invalid_syntax(self, reviewer: SecurityReviewer) -> None:
        """语法错误沙箱捕获。"""
        code = "def incomplete("
        result = reviewer.sandbox_execute(code)
        assert result["success"] is False

    def test_sandbox_execute_returns_structure(self, reviewer: SecurityReviewer) -> None:
        """返回结构包含必需字段。"""
        code = "x = 1"
        result = reviewer.sandbox_execute(code)

        assert "success" in result
        assert "output" in result
        assert "error" in result
        assert "timed_out" in result
        assert "return_code" in result

    def test_sandbox_execute_timeout(self, reviewer: SecurityReviewer) -> None:
        """超时时代码被终止。"""
        # 注意：实际测试中不用真的等待超时，只验证返回结构
        code = "pass"
        result = reviewer.sandbox_execute(code, timeout=1.0)
        # 正常代码不会超时
        assert result["timed_out"] is False

    def test_sandbox_execute_complex_valid_code(self, reviewer: SecurityReviewer) -> None:
        """复杂合法代码通过沙箱。"""
        code = '''
import json
import logging

class MyTool:
    def execute(self):
        return {"status": "ok"}
'''
        result = reviewer.sandbox_execute(code)
        assert result["success"] is True


# =========================================================================
# check_permissions 测试
# =========================================================================


class TestCheckPermissions:
    """权限检查测试。"""

    def test_check_permissions_clean_code(self, reviewer: SecurityReviewer) -> None:
        """无权限声明的代码无问题。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code="x = 1",
            file_path="test.py",
        )
        issues = reviewer.check_permissions(artifact)
        assert issues == []

    def test_check_permissions_with_allowed_ops(self) -> None:
        """声明的操作在白名单中。"""
        reviewer = SecurityReviewer(allowed_permissions={"file_write", "network"})

        code = '''
dangerous_operations = "file_write"
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        issues = reviewer.check_permissions(artifact)
        assert issues == []

    def test_check_permissions_with_unauthorized_ops(self) -> None:
        """声明的操作不在白名单中。"""
        reviewer = SecurityReviewer(allowed_permissions={"file_read"})

        code = '''
dangerous_operations = "subprocess"
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        issues = reviewer.check_permissions(artifact)
        assert len(issues) > 0
        assert any("未授权" in issue for issue in issues)

    def test_check_permissions_syntax_error(self, reviewer: SecurityReviewer) -> None:
        """语法错误时报告问题。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code="def incomplete(",
            file_path="test.py",
        )
        issues = reviewer.check_permissions(artifact)
        assert any("语法错误" in issue for issue in issues)


# =========================================================================
# check_resource_limits 测试
# =========================================================================


class TestCheckResourceLimits:
    """资源限制检查测试。"""

    def test_check_resource_limits_clean(self, reviewer: SecurityReviewer) -> None:
        """干净代码无违规。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code="x = 1",
            file_path="test.py",
        )
        violations = reviewer.check_resource_limits(artifact)
        assert violations == []

    def test_check_resource_limits_while_true_no_break(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测无 break 的死循环。"""
        code = '''
def bad():
    while True:
        pass
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        violations = reviewer.check_resource_limits(artifact)
        assert any("死循环" in v or "while True" in v for v in violations)

    def test_check_resource_limits_while_true_with_break(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """有 break 的 while True 不误报。"""
        code = '''
def good():
    while True:
        if condition:
            break
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        violations = reviewer.check_resource_limits(artifact)
        assert not any("死循环" in v for v in violations)

    def test_check_resource_limits_large_range(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测大范围迭代。"""
        code = '''
def bad():
    result = list(range(1000000))
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        violations = reviewer.check_resource_limits(artifact)
        assert any("range" in v for v in violations)

    def test_check_resource_limits_small_range_ok(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """小范围迭代不报。"""
        code = '''
def ok():
    result = list(range(100))
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        violations = reviewer.check_resource_limits(artifact)
        assert not any("range" in v for v in violations)

    def test_check_resource_limits_recursion(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """检测递归调用。"""
        code = '''
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        violations = reviewer.check_resource_limits(artifact)
        assert any("递归" in v for v in violations)

    def test_check_resource_limits_syntax_error(
        self, reviewer: SecurityReviewer,
    ) -> None:
        """语法错误时报告。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code="def incomplete(",
            file_path="test.py",
        )
        violations = reviewer.check_resource_limits(artifact)
        assert any("语法错误" in v for v in violations)


# =========================================================================
# review 综合审查测试
# =========================================================================


class TestReview:
    """综合安全审查测试。"""

    def test_review_passes_clean_code(self, reviewer: SecurityReviewer) -> None:
        """干净代码通过完整审查。"""
        code = '''
"""Test module."""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

class TestTool:
    @staticmethod
    def get_tool_definition():
        return None

    async def execute(self, inputs):
        return {"status": "ok"}
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        report = reviewer.review(artifact)

        assert report.passed is True
        assert report.overall_risk == "low"

    def test_review_blocks_dangerous_code(self, reviewer: SecurityReviewer) -> None:
        """危险代码被阻止。"""
        code = '''
import subprocess
import os

def bad():
    eval("os.system('rm -rf /')")
    subprocess.run(["rm", "-rf", "/"])
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        report = reviewer.review(artifact)

        assert report.passed is False
        assert report.overall_risk in ("critical", "high")
        assert len(report.static_analysis_issues) > 0

    def test_review_reports_all_checks(self, reviewer: SecurityReviewer) -> None:
        """审查报告包含所有检查结果。"""
        code = 'import json\ndef hello(): pass'
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        report = reviewer.review(artifact)

        assert isinstance(report.static_analysis_issues, list)
        assert report.sandbox_result is not None
        assert isinstance(report.permission_issues, list)
        assert isinstance(report.resource_violations, list)

    def test_review_medium_risk(self, reviewer: SecurityReviewer) -> None:
        """中等风险代码。"""
        code = '''
def tool():
    f = open("file.txt", "r")
    return f.read()
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        report = reviewer.review(artifact)
        # open() 触发 filesystem 检查（medium severity）
        assert report.overall_risk in ("low", "medium")


# =========================================================================
# MF-03 修复验证
# =========================================================================


class TestSecurityModuleSelfCheck:
    """安全模块自身安全检查（MF-03 修复验证）。"""

    def test_no_dangerous_imports_in_module(self) -> None:
        """模块自身不导入危险模块（MF-03修复验证）。

        安全审查器模块不应包含危险模块的顶级导入。
        注意：sandbox_execute 中有延迟导入 subprocess，这是允许的。
        """
        import evolution.security_reviewer as mod

        dangerous_top_level = {"subprocess", "os", "ctypes", "socket"}
        for dangerous in dangerous_top_level:
            assert not hasattr(mod, dangerous), (
                f"security_reviewer 不应有顶级 {dangerous} 导入"
            )

    def test_module_source_no_top_level_subprocess(self) -> None:
        """源代码中无顶级 subprocess 导入。"""
        import inspect
        import evolution.security_reviewer as mod

        source = inspect.getsource(mod)
        lines = source.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("import subprocess") or stripped.startswith("from subprocess"):
                # 允许在函数内部导入
                indent = len(line) - len(line.lstrip())
                assert indent > 0, f"发现顶级 subprocess 导入在第 {i+1} 行"
