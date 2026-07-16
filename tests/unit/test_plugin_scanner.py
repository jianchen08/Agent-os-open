"""统一插件扫描器测试。

覆盖场景：
- 共享管道插件发现（input/output/core 三类）
- 共享 Sidecar 插件发现（system/tools 两类）
- 租户插件发现与共享覆盖语义
- resolve_pipeline_plugin_module 路径解析
- 空目录/无标记目录跳过
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.plugin_scanner import (
    PluginCategory,
    PluginLocation,
    PluginScope,
    resolve_pipeline_plugin_module,
    scan_all_plugins,
    scan_pipeline_plugins,
    scan_sidecar_plugins,
)


@pytest.fixture
def plugins_root(tmp_path: Path) -> Path:
    """创建模拟的插件目录结构。

    结构：
        tmp_path/
        ├── shared/
        │   ├── system/
        │   │   └── svc_a/ (plugin.json)
        │   ├── pipeline/
        │   │   ├── input/
        │   │   │   └── in_a/ (plugin.py)
        │   │   ├── output/
        │   │   │   └── out_a/ (plugin.py)
        │   │   └── core/
        │   │       └── core_a/ (plugin.py)
        │   └── tools/
        │       └── tool_a/ (plugin.json)
        └── tenants/
            └── tenant1/
                ├── pipeline/
                │   └── input/
                │       └── in_a/ (plugin.py)  # 覆盖共享同名
                └── system/
                    └── svc_b/ (plugin.json)
    """
    root = tmp_path

    # 共享管道插件
    (root / "shared" / "pipeline" / "input" / "in_a").mkdir(parents=True)
    (root / "shared" / "pipeline" / "input" / "in_a" / "plugin.py").write_text("# stub")
    (root / "shared" / "pipeline" / "input" / "in_a" / "__init__.py").write_text("")

    (root / "shared" / "pipeline" / "output" / "out_a").mkdir(parents=True)
    (root / "shared" / "pipeline" / "output" / "out_a" / "plugin.py").write_text("# stub")
    (root / "shared" / "pipeline" / "output" / "out_a" / "__init__.py").write_text("")

    (root / "shared" / "pipeline" / "core" / "core_a").mkdir(parents=True)
    (root / "shared" / "pipeline" / "core" / "core_a" / "plugin.py").write_text("# stub")
    (root / "shared" / "pipeline" / "core" / "core_a" / "__init__.py").write_text("")

    # 共享系统插件
    (root / "shared" / "system" / "svc_a").mkdir(parents=True)
    (root / "shared" / "system" / "svc_a" / "plugin.json").write_text('{"id": "svc_a"}')

    # 共享工具插件
    (root / "shared" / "tools" / "tool_a").mkdir(parents=True)
    (root / "shared" / "tools" / "tool_a" / "plugin.json").write_text('{"id": "tool_a"}')

    # 租户插件
    (root / "tenants" / "tenant1" / "pipeline" / "input" / "in_a").mkdir(parents=True)
    (root / "tenants" / "tenant1" / "pipeline" / "input" / "in_a" / "plugin.py").write_text("# tenant override")
    (root / "tenants" / "tenant1" / "pipeline" / "input" / "in_a" / "__init__.py").write_text("")

    (root / "tenants" / "tenant1" / "system" / "svc_b").mkdir(parents=True)
    (root / "tenants" / "tenant1" / "system" / "svc_b" / "plugin.json").write_text('{"id": "svc_b"}')

    return root


# ── scan_pipeline_plugins ─────────────────────────────


class TestScanPipelinePlugins:
    """管道插件扫描测试。"""

    def test_discovers_shared_pipeline_plugins(self, plugins_root: Path) -> None:
        """共享管道插件全部三类都能被发现。"""
        locations = scan_pipeline_plugins(plugins_root=plugins_root)

        names = {loc.plugin_name for loc in locations}
        assert "in_a" in names
        assert "out_a" in names
        assert "core_a" in names

    def test_all_shared_locations_have_shared_scope(self, plugins_root: Path) -> None:
        """不传 tenant_id 时，所有结果都是 SHARED 作用域。"""
        locations = scan_pipeline_plugins(plugins_root=plugins_root)
        for loc in locations:
            assert loc.scope == PluginScope.SHARED

    def test_correct_category_assignment(self, plugins_root: Path) -> None:
        """每个插件的分类正确对应。"""
        locations = scan_pipeline_plugins(plugins_root=plugins_root)
        by_name = {loc.plugin_name: loc for loc in locations}

        assert by_name["in_a"].category == PluginCategory.PIPELINE_INPUT
        assert by_name["out_a"].category == PluginCategory.PIPELINE_OUTPUT
        assert by_name["core_a"].category == PluginCategory.PIPELINE_CORE

    def test_tenant_plugin_overrides_shared(self, plugins_root: Path) -> None:
        """租户同名插件覆盖共享插件。"""
        locations = scan_pipeline_plugins(tenant_id="tenant1", plugins_root=plugins_root)
        in_a_locations = [loc for loc in locations if loc.plugin_name == "in_a"]

        # 只有一个 in_a（租户覆盖了共享）
        assert len(in_a_locations) == 1
        assert in_a_locations[0].scope == PluginScope.TENANT
        assert in_a_locations[0].tenant_id == "tenant1"

    def test_tenant_adds_new_plugins(self, plugins_root: Path) -> None:
        """租户独有的插件会被追加。"""
        locations = scan_pipeline_plugins(tenant_id="tenant1", plugins_root=plugins_root)
        names = {loc.plugin_name for loc in locations}
        # 共享的 out_a 和 core_a 仍然存在
        assert "out_a" in names
        assert "core_a" in names


# ── scan_sidecar_plugins ──────────────────────────────


class TestScanSidecarPlugins:
    """Sidecar 插件扫描测试。"""

    def test_discovers_shared_system_plugins(self, plugins_root: Path) -> None:
        """共享系统服务插件可被发现。"""
        locations = scan_sidecar_plugins(plugins_root=plugins_root)
        names = {loc.plugin_name for loc in locations}
        assert "svc_a" in names

    def test_discovers_shared_tools_plugins(self, plugins_root: Path) -> None:
        """共享工具插件可被发现。"""
        locations = scan_sidecar_plugins(plugins_root=plugins_root)
        names = {loc.plugin_name for loc in locations}
        assert "tool_a" in names

    def test_correct_category_for_sidecar(self, plugins_root: Path) -> None:
        """Sidecar 插件分类正确。"""
        locations = scan_sidecar_plugins(plugins_root=plugins_root)
        by_name = {loc.plugin_name: loc for loc in locations}

        assert by_name["svc_a"].category == PluginCategory.SYSTEM
        assert by_name["tool_a"].category == PluginCategory.TOOLS

    def test_tenant_sidecar_plugins_discovered(self, plugins_root: Path) -> None:
        """租户 Sidecar 插件可被发现。"""
        locations = scan_sidecar_plugins(tenant_id="tenant1", plugins_root=plugins_root)
        names = {loc.plugin_name for loc in locations}
        assert "svc_b" in names


# ── scan_all_plugins ──────────────────────────────────


class TestScanAllPlugins:
    """合并扫描测试。"""

    def test_returns_all_categories(self, plugins_root: Path) -> None:
        """合并扫描返回所有分类的插件。"""
        locations = scan_all_plugins(plugins_root=plugins_root)
        categories = {loc.category for loc in locations}
        assert PluginCategory.SYSTEM in categories
        assert PluginCategory.PIPELINE_INPUT in categories
        assert PluginCategory.PIPELINE_OUTPUT in categories
        assert PluginCategory.PIPELINE_CORE in categories
        assert PluginCategory.TOOLS in categories


# ── resolve_pipeline_plugin_module ────────────────────


class TestResolvePipelinePluginModule:
    """模块路径解析测试。"""

    def test_resolve_shared_input_plugin(self, plugins_root: Path) -> None:
        """共享 input 插件解析为正确模块路径。"""
        result = resolve_pipeline_plugin_module(
            "in_a", "input", plugins_root=plugins_root
        )
        assert result == "plugins.shared.pipeline.input.in_a"

    def test_resolve_shared_output_plugin(self, plugins_root: Path) -> None:
        """共享 output 插件解析为正确模块路径。"""
        result = resolve_pipeline_plugin_module(
            "out_a", "output", plugins_root=plugins_root
        )
        assert result == "plugins.shared.pipeline.output.out_a"

    def test_resolve_tenant_plugin_overrides_shared(self, plugins_root: Path) -> None:
        """租户插件优先于共享。"""
        result = resolve_pipeline_plugin_module(
            "in_a", "input", tenant_id="tenant1", plugins_root=plugins_root
        )
        assert result == "plugins.tenants.tenant1.pipeline.input.in_a"

    def test_resolve_nonexistent_plugin_returns_none(self, plugins_root: Path) -> None:
        """不存在的插件返回 None。"""
        result = resolve_pipeline_plugin_module(
            "no_such_plugin", "input", plugins_root=plugins_root
        )
        assert result is None


# ── 边界场景 ──────────────────────────────────────────


class TestEdgeCases:
    """边界场景测试。"""

    def test_empty_directory(self, tmp_path: Path) -> None:
        """空目录返回空列表。"""
        (tmp_path / "shared" / "pipeline" / "input").mkdir(parents=True)
        locations = scan_pipeline_plugins(plugins_root=tmp_path)
        assert locations == []

    def test_directory_without_marker_skipped(self, tmp_path: Path) -> None:
        """没有标记文件的目录被跳过。"""
        plugin_dir = tmp_path / "shared" / "pipeline" / "input" / "no_marker"
        plugin_dir.mkdir(parents=True)
        # 只有普通文件，没有 plugin.py/__init__.py/plugin.json/server.py
        (plugin_dir / "readme.md").write_text("not a plugin")

        locations = scan_pipeline_plugins(plugins_root=tmp_path)
        assert locations == []

    def test_nonexistent_plugins_root(self, tmp_path: Path) -> None:
        """不存在的根目录返回空列表。"""
        locations = scan_pipeline_plugins(plugins_root=tmp_path / "nonexistent")
        assert locations == []

    def test_hidden_directories_skipped(self, tmp_path: Path) -> None:
        """隐藏目录被跳过。"""
        plugin_dir = tmp_path / "shared" / "pipeline" / "input" / "_internal"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.py").write_text("# internal")

        locations = scan_pipeline_plugins(plugins_root=tmp_path)
        names = {loc.plugin_name for loc in locations}
        assert "_internal" not in names
