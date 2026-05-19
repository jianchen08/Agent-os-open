"""插件验证器测试。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools.plugin_validator import (
    PluginValidator,
    PluginValidationReport,
    ValidationResult,
    Severity,
)


@pytest.fixture
def tmp_plugins_dir(tmp_path: Path) -> Path:
    """创建临时插件目录结构。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    for ptype in ("input", "output", "core"):
        (plugins_dir / ptype).mkdir()
    return plugins_dir


@pytest.fixture
def validator(tmp_plugins_dir: Path) -> PluginValidator:
    """创建使用临时目录的验证器。"""
    return PluginValidator(plugins_dir=tmp_plugins_dir)


def _write_plugin(
    plugins_dir: Path,
    plugin_type: str,
    plugin_name: str,
    source: str,
) -> Path:
    """写入插件源文件。"""
    plugin_dir = plugins_dir / plugin_type / plugin_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    main_file = plugin_dir / f"{plugin_name}.py"
    main_file.write_text(textwrap.dedent(source), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    return plugin_dir


# ── 合规插件测试 ──────────────────────────────────────


class TestCompliantInputPlugin:
    """合规的 Input 插件应通过验证。"""

    COMPLIANT_INPUT = '''\
        """test_plugin 插件 — 测试插件。"""

        import logging
        from typing import Any
        from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
        from pipeline.types import ErrorPolicy

        logger = logging.getLogger(__name__)

        class TestPlugin(IInputPlugin):
            """测试插件。"""

            error_policy: ErrorPolicy = ErrorPolicy.ABORT

            def __init__(self, config: dict[str, Any] | None = None) -> None:
                self._config = config or {}
                self._enabled = self._config.get("enabled", True)

            @property
            def name(self) -> str:
                return "test_plugin"

            @property
            def priority(self) -> int:
                return 50

            async def execute(self, ctx: PluginContext) -> PluginResult:
                return PluginResult(state_updates={"test.result": "ok"})
    '''

    def test_compliant_plugin_passes(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """合规插件应通过所有验证。"""
        _write_plugin(tmp_plugins_dir, "input", "test_plugin", self.COMPLIANT_INPUT)
        report = validator.validate_plugin("input", "test_plugin")
        assert report.passed, f"合规插件验证失败: {[r.message for r in report.results if r.severity == Severity.ERROR]}"
        assert report.error_count == 0


class TestCompliantOutputPlugin:
    """合规的 Output 插件应通过验证。"""

    COMPLIANT_OUTPUT = '''\
        """test_output 插件 — 测试输出。"""

        import logging
        from typing import Any
        from pipeline.plugin import IOutputPlugin, PluginContext, OutputResult
        from pipeline.types import ErrorPolicy

        class TestOutputPlugin(IOutputPlugin):
            """测试输出。"""
            error_policy: ErrorPolicy = ErrorPolicy.SKIP

            def __init__(self, config: dict[str, Any] | None = None) -> None:
                self._config = config or {}

            @property
            def name(self) -> str:
                return "test_output"

            @property
            def priority(self) -> int:
                return 60

            async def execute(self, ctx: PluginContext) -> OutputResult:
                return OutputResult()
    '''

    def test_compliant_output_passes(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """合规 Output 插件应通过。"""
        _write_plugin(tmp_plugins_dir, "output", "test_output", self.COMPLIANT_OUTPUT)
        report = validator.validate_plugin("output", "test_output")
        assert report.passed


# ── 不合规插件测试 ──────────────────────────────────────


class TestMissingBaseClass:
    """缺少基类继承。"""

    def test_no_base_class(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """不继承 IInputPlugin 应报错。"""
        source = '''\
            """bad plugin."""

            class BadPlugin:
                @property
                def name(self) -> str:
                    return "bad_plugin"

                @property
                def priority(self) -> int:
                    return 50

                async def execute(self, ctx):
                    return {}
        '''
        _write_plugin(tmp_plugins_dir, "input", "bad_plugin", source)
        report = validator.validate_plugin("input", "bad_plugin")
        assert not report.passed
        assert any(r.rule_id == "IFACE-001" for r in report.results)


class TestMissingExecute:
    """缺少 execute 方法。"""

    def test_no_execute(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """缺少 execute 方法应报错。"""
        source = '''\
            """no execute."""

            from pipeline.plugin import IInputPlugin
            from pipeline.types import ErrorPolicy

            class NoExecPlugin(IInputPlugin):
                error_policy: ErrorPolicy = ErrorPolicy.ABORT

                @property
                def name(self) -> str:
                    return "no_exec"

                @property
                def priority(self) -> int:
                    return 50
        '''
        _write_plugin(tmp_plugins_dir, "input", "no_exec", source)
        report = validator.validate_plugin("input", "no_exec")
        assert not report.passed
        assert any(r.rule_id == "IFACE-004" for r in report.results)


class TestMissingNameProperty:
    """缺少 name 属性。"""

    def test_no_name(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """缺少 name 属性应报错。"""
        source = '''\
            """no name."""

            from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
            from pipeline.types import ErrorPolicy

            class NoNamePlugin(IInputPlugin):
                error_policy: ErrorPolicy = ErrorPolicy.ABORT

                @property
                def priority(self) -> int:
                    return 50

                async def execute(self, ctx: PluginContext) -> PluginResult:
                    return PluginResult()
        '''
        _write_plugin(tmp_plugins_dir, "input", "no_name", source)
        report = validator.validate_plugin("input", "no_name")
        assert not report.passed
        assert any(r.rule_id == "IFACE-002" for r in report.results)


class TestMissingPriority:
    """缺少 priority 属性。"""

    def test_no_priority(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """缺少 priority 属性应报错。"""
        source = '''\
            """no priority."""

            from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
            from pipeline.types import ErrorPolicy

            class NoPriPlugin(IInputPlugin):
                error_policy: ErrorPolicy = ErrorPolicy.ABORT

                @property
                def name(self) -> str:
                    return "no_pri"

                async def execute(self, ctx: PluginContext) -> PluginResult:
                    return PluginResult()
        '''
        _write_plugin(tmp_plugins_dir, "input", "no_pri", source)
        report = validator.validate_plugin("input", "no_pri")
        assert not report.passed
        assert any(r.rule_id == "IFACE-003" for r in report.results)


class TestUnsafeFunctions:
    """不安全函数检测。"""

    def test_eval_usage(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """使用 eval() 应报错。"""
        source = '''\
            """unsafe plugin."""

            from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
            from pipeline.types import ErrorPolicy

            class UnsafePlugin(IInputPlugin):
                error_policy: ErrorPolicy = ErrorPolicy.ABORT

                @property
                def name(self) -> str:
                    return "unsafe_plugin"

                @property
                def priority(self) -> int:
                    return 50

                async def execute(self, ctx: PluginContext) -> PluginResult:
                    result = eval("1+1")
                    return PluginResult()
        '''
        _write_plugin(tmp_plugins_dir, "input", "unsafe_plugin", source)
        report = validator.validate_plugin("input", "unsafe_plugin")
        assert not report.passed
        assert any(r.rule_id == "SEC-001" for r in report.results)


class TestInvalidErrorPolicy:
    """无效的错误策略。"""

    def test_invalid_policy(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """无效的 error_policy 应报错。"""
        source = '''\
            """bad policy."""

            from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
            from pipeline.types import ErrorPolicy

            class BadPolicyPlugin(IInputPlugin):
                error_policy: ErrorPolicy = ErrorPolicy.INVALID

                @property
                def name(self) -> str:
                    return "bad_policy"

                @property
                def priority(self) -> int:
                    return 50

                async def execute(self, ctx: PluginContext) -> PluginResult:
                    return PluginResult()
        '''
        _write_plugin(tmp_plugins_dir, "input", "bad_policy", source)
        report = validator.validate_plugin("input", "bad_policy")
        assert not report.passed
        assert any(r.rule_id == "POLICY-001" for r in report.results)


class TestNonAsyncExecute:
    """非 async execute 方法。"""

    def test_sync_execute(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """execute 不是 async 应报错。"""
        source = '''\
            """sync execute."""

            from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
            from pipeline.types import ErrorPolicy

            class SyncPlugin(IInputPlugin):
                error_policy: ErrorPolicy = ErrorPolicy.ABORT

                @property
                def name(self) -> str:
                    return "sync_plugin"

                @property
                def priority(self) -> int:
                    return 50

                def execute(self, ctx: PluginContext) -> PluginResult:
                    return PluginResult()
        '''
        _write_plugin(tmp_plugins_dir, "input", "sync_plugin", source)
        report = validator.validate_plugin("input", "sync_plugin")
        assert not report.passed
        assert any(r.rule_id == "IFACE-005" for r in report.results)


# ── validate_all 测试 ─────────────────────────────────


class TestValidateAll:
    """批量验证测试。"""

    def test_validate_all_empty(self, validator: PluginValidator) -> None:
        """空目录应返回空报告。"""
        reports = validator.validate_all()
        assert reports == []

    def test_validate_multiple(self, validator: PluginValidator, tmp_plugins_dir: Path) -> None:
        """应能验证多个插件。"""
        _write_plugin(tmp_plugins_dir, "input", "plugin_a", TestCompliantInputPlugin.COMPLIANT_INPUT.replace("test_plugin", "plugin_a").replace("TestPlugin", "PluginA"))
        _write_plugin(tmp_plugins_dir, "output", "plugin_b", TestCompliantOutputPlugin.COMPLIANT_OUTPUT.replace("test_output", "plugin_b").replace("TestOutput", "PluginB"))

        reports = validator.validate_all()
        assert len(reports) == 2


# ── 报告格式测试 ─────────────────────────────────────


class TestReportFormat:
    """报告格式测试。"""

    def test_summary_format(self) -> None:
        """摘要应包含关键信息。"""
        report = PluginValidationReport(
            plugin_name="test_plugin",
            plugin_type="input",
            plugin_path="/path/to/plugin",
        )
        report.add(ValidationResult(
            rule_id="TEST-001",
            severity=Severity.WARNING,
            message="测试警告",
        ))
        summary = report.summary()
        assert "test_plugin" in summary
        assert "input" in summary
        assert "⚠️" in summary
        assert "TEST-001" in summary
        assert "测试警告" in summary
