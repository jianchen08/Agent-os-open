"""工具迁移验证测试——验证简单工具插件的导入和基本功能。

测试覆盖：
1. plugin.json 格式正确
2. server.py 可导入（create_plugin 返回 AgentOSPlugin 实例）
3. TOOL_REGISTRY 包含全部 11 个工具
4. 各工具函数可被调用并返回正确结构
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# 将 simple 工具目录加入 sys.path
SIMPLE_DIR = Path(__file__).resolve().parent.parent / "simple"
sys.path.insert(0, str(SIMPLE_DIR))

# 将 SDK 目录加入 sys.path
SDK_DIR = Path(__file__).resolve().parents[4] / "plugins" / "sdk" / "src"
sys.path.insert(0, str(SDK_DIR))


# ── plugin.json 校验 ──────────────────────────────────


class TestSimplePluginJson:
    """验证 simple/plugin.json 格式正确。"""

    def test_plugin_json_exists(self):
        assert (SIMPLE_DIR / "plugin.json").exists()

    def test_plugin_json_valid_json(self):
        data = json.loads((SIMPLE_DIR / "plugin.json").read_text(encoding="utf-8"))
        assert data["id"] == "simple_tools"
        assert data["plugin_type"] == "tool"
        assert data["host_type"] == "sidecar"
        assert "entry" in data

    def test_plugin_json_has_11_tools(self):
        data = json.loads((SIMPLE_DIR / "plugin.json").read_text(encoding="utf-8"))
        tools = data["capabilities"]["tools"]
        assert len(tools) == 11
        tool_names = {t["name"] for t in tools}
        expected = {
            "unit_converter", "scientific_calculator", "yaml_validate",
            "binary_converter", "ide_get_selection", "ide_open_file",
            "ide_show_diff", "state_update", "compatibility_checker",
            "read_execution_detail", "register_resource",
        }
        assert tool_names == expected


# ── server.py 导入校验 ──────────────────────────────────


class TestSimpleServerImport:
    """验证 server.py 可正确导入并构建 plugin。"""

    def test_create_plugin_returns_agentosplugin(self):
        from server import create_plugin
        from lingxi_plugin_sdk import AgentOSPlugin

        plugin = create_plugin()
        assert isinstance(plugin, AgentOSPlugin)
        assert plugin.name == "simple_tools"

    def test_tool_registry_has_11_tools(self):
        from server import TOOL_REGISTRY

        assert len(TOOL_REGISTRY) == 11

    def test_all_tools_registered(self):
        from server import create_plugin

        plugin = create_plugin()
        registered = set(plugin._tools.keys())
        expected = {
            "unit_converter", "scientific_calculator", "yaml_validate",
            "binary_converter", "ide_get_selection", "ide_open_file",
            "ide_show_diff", "state_update", "compatibility_checker",
            "read_execution_detail", "register_resource",
        }
        assert registered == expected


# ── 工具函数功能校验 ──────────────────────────────────


class TestUnitConverter:
    """单位换算工具测试。"""

    @pytest.mark.asyncio
    async def test_length_conversion(self):
        from converter_tools import unit_converter

        result = await unit_converter(100, "m", "km", "length")
        assert "result" in result
        assert result["result"] == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_weight_conversion(self):
        from converter_tools import unit_converter

        result = await unit_converter(1, "kg", "g", "weight")
        assert result["result"] == pytest.approx(1000.0)

    @pytest.mark.asyncio
    async def test_temperature_conversion(self):
        from converter_tools import unit_converter

        result = await unit_converter(0, "C", "F", "temperature")
        assert result["result"] == pytest.approx(32.0)

    @pytest.mark.asyncio
    async def test_unsupported_unit(self):
        from converter_tools import unit_converter

        result = await unit_converter(1, "foo", "bar", "length")
        assert "error" in result


class TestScientificCalculator:
    """科学计算器测试。"""

    @pytest.mark.asyncio
    async def test_evaluate_sin(self):
        from calc_tools import scientific_calculator

        result = await scientific_calculator(operation="evaluate", func="sin", value=30)
        assert "result" in result
        assert result["result"] == pytest.approx(0.5, abs=1e-6)

    @pytest.mark.asyncio
    async def test_calculate_expression(self):
        from calc_tools import scientific_calculator

        result = await scientific_calculator(operation="calculate", expression="2 + 3 * 4")
        assert result["result"] == 14

    @pytest.mark.asyncio
    async def test_evaluate_sqrt(self):
        from calc_tools import scientific_calculator

        result = await scientific_calculator(operation="evaluate", func="sqrt", value=16)
        assert result["result"] == 4


class TestYamlValidate:
    """YAML 校验工具测试。"""

    @pytest.mark.asyncio
    async def test_valid_yaml_content(self):
        from system_tools import yaml_validate

        result = await yaml_validate(content="name: test\nvalue: 123")
        assert result["valid"] is True
        assert result["parsed"]["name"] == "test"

    @pytest.mark.asyncio
    async def test_invalid_yaml_syntax(self):
        from system_tools import yaml_validate

        result = await yaml_validate(content="name: [unclosed bracket\n")
        assert result["valid"] is False

    @pytest.mark.asyncio
    async def test_missing_required_fields(self):
        from system_tools import yaml_validate

        result = await yaml_validate(
            content="name: test",
            required_fields=["name", "version"],
        )
        assert result["valid"] is False
        assert any("version" in e for e in result["errors"])


class TestStateUpdate:
    """工作流状态更新测试。"""

    @pytest.mark.asyncio
    async def test_direct_assignment(self):
        from workflow_tools import state_update

        result = await state_update(updates={"key1": "value1", "count": 42})
        assert result["success"] is True
        assert result["updates"]["key1"] == "value1"
        assert result["updates"]["count"] == 42

    @pytest.mark.asyncio
    async def test_increment_operation(self):
        from workflow_tools import state_update

        result = await state_update(
            updates={"retry_count": {"operation": "increment", "value": 1}}
        )
        assert result["success"] is True
        assert result["updates"]["retry_count"] == 1


class TestCompatibilityChecker:
    """兼容性检查工具测试。"""

    @pytest.mark.asyncio
    async def test_compatible_resources(self):
        from workflow_tools import compatibility_checker

        result = await compatibility_checker(
            original_resource={"resource_info": {"name": "test"}},
            modified_resource={"resource_info": {"name": "test"}},
        )
        assert result["compatible"] is True

    @pytest.mark.asyncio
    async def test_breaking_change_field_removed(self):
        from workflow_tools import compatibility_checker

        result = await compatibility_checker(
            original_resource={"resource_info": {"name": "test", "id": "123"}},
            modified_resource={"resource_info": {"name": "test"}},
        )
        assert result["compatible"] is False
        assert any(
            bc["type"] == "field_removed" for bc in result["breaking_changes"]
        )


# ── 外部 MCP plugin.json 校验 ──────────────────────────


class TestExternalMcpPluginJson:
    """验证 7 个外部 MCP 工具的 plugin.json 格式。"""

    EXTERNAL_DIR = Path(__file__).resolve().parent.parent / "external_mcp"

    @pytest.mark.parametrize("tool_name", [
        "web_search", "browser_test", "design_generate",
        "design_review", "mcp_registry", "smithery", "resource_search",
    ])
    def test_external_mcp_plugin_json(self, tool_name):
        plugin_json_path = self.EXTERNAL_DIR / tool_name / "plugin.json"
        assert plugin_json_path.exists(), f"Missing plugin.json for {tool_name}"

        data = json.loads(plugin_json_path.read_text(encoding="utf-8"))
        assert data["entry"] == "mcp:external", f"{tool_name} should use mcp:external entry"
        assert "mcp_endpoint" in data, f"{tool_name} should have mcp_endpoint"
        assert data["mcp_endpoint"]["transport"] in ("stdio", "http")
        assert "capabilities" in data
        assert len(data["capabilities"]["tools"]) >= 1


# ── 复杂工具 plugin.json + server.py 存在性校验 ──────────


class TestComplexToolsStructure:
    """验证 18 个复杂工具的目录结构。"""

    TOOLS_DIR = Path(__file__).resolve().parent.parent

    @pytest.mark.parametrize("tool_dir", [
        "bash", "download", "resource_merge",
        "task", "task_submit", "task_evaluate",
        "test_ext", "search", "triggers_ext",
        "media", "lsp", "memory",
        "human", "web_ext", "hot_swap",
    ])
    def test_complex_tool_has_plugin_json(self, tool_dir):
        assert (self.TOOLS_DIR / tool_dir / "plugin.json").exists(), \
            f"Missing plugin.json for {tool_dir}"

    @pytest.mark.parametrize("tool_dir", [
        "bash", "download", "resource_merge",
        "task", "task_submit", "task_evaluate",
        "test_ext", "search", "triggers_ext",
        "media", "lsp", "memory",
        "human", "web_ext", "hot_swap",
    ])
    def test_complex_tool_has_server_py(self, tool_dir):
        assert (self.TOOLS_DIR / tool_dir / "server.py").exists(), \
            f"Missing server.py for {tool_dir}"

    @pytest.mark.parametrize("tool_dir", [
        "bash", "download", "resource_merge",
        "task", "task_submit", "task_evaluate",
        "test_ext", "search", "triggers_ext",
        "media", "lsp", "memory",
        "human", "web_ext", "hot_swap",
    ])
    def test_complex_tool_has_source_code(self, tool_dir):
        """验证复杂工具目录下至少有一个 .py 源代码文件（tool.py 或类似）。"""
        tool_path = self.TOOLS_DIR / tool_dir
        py_files = [
            f for f in tool_path.iterdir()
            if f.suffix == ".py" and f.name not in ("server.py", "__init__.py")
        ]
        assert len(py_files) >= 1, \
            f"{tool_dir} should have at least 1 source .py file (copied from 0.1)"
