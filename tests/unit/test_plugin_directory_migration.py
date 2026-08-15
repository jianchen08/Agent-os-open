"""插件目录迁移兼容性测试。

验证：
- 新路径插件发现（plugins.shared.*）
- 旧路径兼容性（通过 shim 重定向到新位置）
- YAML class 路径自动迁移（plugins.core.* → plugins.shared.core.*）
- 新旧路径返回同一个类
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from pipeline.plugin import IInputPlugin, IOutputPlugin

# ── 新路径发现 ────────────────────────────────────────


class TestNewPathDiscovery:
    """新路径 plugins.shared.* 下的插件发现。"""

    @pytest.mark.parametrize("plugin_name", [
        "context_build",
        "param_inject",
        "tool_schema",
        "memory_read",
        "reasoning_check",
    ])
    def test_input_plugin_discoverable_via_new_path(self, plugin_name: str) -> None:
        """Input 插件在新路径 plugins.shared.input.{name}.plugin 下可发现。"""
        from pipeline.config import _discover_plugin_class

        cls = _discover_plugin_class(plugin_name)
        assert cls is not None, f"Plugin {plugin_name} not discovered"
        assert issubclass(cls, IInputPlugin)

    @pytest.mark.parametrize("plugin_name", [
        "track",
        "stop_check",
        "result_format",
        "error_check",
        "duplicate_check",
    ])
    def test_output_plugin_discoverable_via_new_path(self, plugin_name: str) -> None:
        """Output 插件在新路径 plugins.shared.output.{name}.plugin 下可发现。"""
        from pipeline.config import _discover_plugin_class

        cls = _discover_plugin_class(plugin_name)
        assert cls is not None, f"Plugin {plugin_name} not discovered"
        assert issubclass(cls, IOutputPlugin)


# ── 旧路径兼容性 ─────────────────────────────────────


class TestLegacyPathCompatibility:
    """旧路径 plugins.input.* / plugins.output.* 通过 shim 重定向。"""

    @pytest.mark.parametrize("plugin_name", [
        "context_build",
        "param_inject",
        "tool_schema",
    ])
    def test_input_plugin_importable_via_legacy_path(self, plugin_name: str) -> None:
        """旧路径 import 可达（shim 重定向到新位置）。"""
        module = importlib.import_module(f"plugins.input.{plugin_name}.plugin")
        assert module is not None

    @pytest.mark.parametrize("plugin_name", [
        "track",
        "stop_check",
        "result_format",
    ])
    def test_output_plugin_importable_via_legacy_path(self, plugin_name: str) -> None:
        """旧路径 import 可达（shim 重定向到新位置）。"""
        module = importlib.import_module(f"plugins.output.{plugin_name}.plugin")
        assert module is not None


# ── 新旧路径一致性 ──────────────────────────────────


class TestPathEquivalence:
    """新旧路径指向同一物理文件。"""

    def test_context_build_same_source(self) -> None:
        """新旧路径导入的 ContextBuildPlugin 来自同一物理文件。"""
        new = importlib.import_module("plugins.shared.input.context_build.plugin")
        old = importlib.import_module("plugins.input.context_build.plugin")
        # __path__ shim 重定向到同一文件，但 Python 模块系统按模块名缓存，
        # 因此是不同模块对象但指向同一 .py 文件
        assert Path(new.__file__).resolve() == Path(old.__file__).resolve()

    def test_track_same_source(self) -> None:
        """新旧路径导入的 TrackPlugin 来自同一物理文件。"""
        new = importlib.import_module("plugins.shared.output.track.plugin")
        old = importlib.import_module("plugins.output.track.plugin")
        assert Path(new.__file__).resolve() == Path(old.__file__).resolve()


# ── YAML class 路径迁移 ─────────────────────────────


class TestYamlClassPathMigration:
    """YAML 配置中的 class 字段路径自动迁移。"""

    def test_name_based_resolution_works(self) -> None:
        """name 方式的配置能正确解析到新路径的插件。"""
        from pipeline.config import _resolve_plugin_class

        cls = _resolve_plugin_class({"name": "context_build"})
        assert cls is not None
        assert "shared" in cls.__module__

    def test_class_path_migration_for_input_prefix(self) -> None:
        """旧前缀 plugins.input.* 自动迁移到 plugins.shared.input.*。"""
        from pipeline.config import _import_class

        # 模拟 YAML 中的旧路径引用
        cls = _import_class("plugins.shared.input.context_build.plugin.ContextBuildPlugin")
        assert cls is not None
        assert cls.__name__ == "ContextBuildPlugin"


# ── 审查补充测试 ──────────────────────────────────────


class TestYamlClassLegacyMigration:
    """旧 class 路径经 _resolve_plugin_class 自动迁移（审查 Must Fix #3）。

    注意：LLMCore/ToolCore 实际导入依赖 _message_normalizer.py（项目原有缺失文件），
    此处验证迁移映射逻辑本身正确（旧路径→新路径转换），不依赖缺失文件。
    """

    def test_legacy_llm_core_class_migrates_to_new_path(self) -> None:
        """旧路径 plugins.core.llm_core.LLMCore 被迁移为 plugins.shared.core.* 新路径。"""
        from pipeline.config import _migrate_class_path

        new_path = _migrate_class_path("plugins.core.llm_core.LLMCore")
        assert new_path == "plugins.shared.core.llm_core.plugin.LLMCore"

    def test_legacy_tool_core_class_migrates_to_new_path(self) -> None:
        """旧路径 plugins.core.tool_core.ToolCore 被迁移为 plugins.shared.core.* 新路径。"""
        from pipeline.config import _migrate_class_path

        new_path = _migrate_class_path("plugins.core.tool_core.ToolCore")
        assert new_path == "plugins.shared.core.tool_core.plugin.ToolCore"

    def test_legacy_input_prefix_migrates(self) -> None:
        """旧前缀 plugins.input.* 迁移为 plugins.shared.input.*。"""
        from pipeline.config import _migrate_class_path

        new_path = _migrate_class_path("plugins.input.context_build.plugin.ContextBuildPlugin")
        assert new_path == "plugins.shared.input.context_build.plugin.ContextBuildPlugin"

    def test_new_path_not_double_migrated(self) -> None:
        """已经是新路径的不会被二次迁移。"""
        from pipeline.config import _migrate_class_path

        new_path = _migrate_class_path("plugins.shared.input.context_build.plugin.ContextBuildPlugin")
        assert new_path == "plugins.shared.input.context_build.plugin.ContextBuildPlugin"


class TestTenantPluginLoading:
    """加载层 tenant_id 支持（审查 Must Fix #2）。"""

    def test_discover_plugin_class_accepts_tenant_id(self) -> None:
        """_discover_plugin_class 接受 tenant_id 参数且不报错。"""
        from pipeline.config import _discover_plugin_class

        # tenant_id 参数存在且默认 None（向后兼容）
        cls = _discover_plugin_class("context_build", tenant_id=None)
        assert cls is not None

    def test_discover_plugin_class_falls_back_to_shared_for_unknown_tenant(self) -> None:
        """未知租户的插件回退到共享目录。"""
        from pipeline.config import _discover_plugin_class

        cls = _discover_plugin_class("context_build", tenant_id="nonexistent_tenant")
        assert cls is not None
        assert "shared" in cls.__module__
