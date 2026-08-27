# @feature: FP-MIGR 0.1→0.2 迁移清理 | @vision: V3 可嵌入 | @ci: python-coverage
"""工具迁移验证测试——验证简单工具插件的导入和基本功能。

测试覆盖：
1. plugin.json 格式正确
2. server.py 可导入（create_plugin 返回 AgentOSPlugin 实例）
3. TOOL_REGISTRY 声明现役 2 工具（yaml_validate / read_execution_detail）
4. 各工具函数可被调用并返回正确结构
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


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

    def test_plugin_json_declares_live_tools(self):
        data = json.loads((SIMPLE_DIR / "plugin.json").read_text(encoding="utf-8"))
        tools = data["capabilities"]["tools"]
        # 8 个 0.1 内置小工具已随 bash/文件工具面收编退役，现役 2 个
        assert len(tools) == 2
        tool_names = {t["name"] for t in tools}
        expected = {"yaml_validate", "read_execution_detail"}
        assert tool_names == expected


# ── server.py 导入校验 ──────────────────────────────────


def _load_simple_server() -> Any:
    """按显式路径加载 simple 插件 server 模块。

    不能用裸 `from server import ...`：同一 pytest 进程里其它插件的
    server.py 会把自身目录插入 sys.path[0]，裸名 `server` 会被劫持到
    错误的插件（如 monitoring/server.py）。显式路径 + 唯一模块名可隔离。
    """
    mod_name = "simple_plugin_server_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    import importlib

    spec = importlib.util.spec_from_file_location(mod_name, SIMPLE_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "cannot load simple plugin server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestSimpleServerImport:
    """验证 server.py 可正确导入并构建 plugin。"""

    def test_create_plugin_returns_agentosplugin(self):
        from agentos_plugin_sdk import AgentOSPlugin

        plugin = _load_simple_server().create_plugin()
        assert isinstance(plugin, AgentOSPlugin)
        assert plugin.name == "simple_tools"

    def test_tool_registry_has_live_tools(self):
        assert len(_load_simple_server().TOOL_REGISTRY) == 2

    def test_all_tools_registered(self):
        plugin = _load_simple_server().create_plugin()
        registered = set(plugin._tools.keys())
        expected = {"yaml_validate", "read_execution_detail"}
        assert registered == expected


# ── 工具函数功能校验 ──────────────────────────────────


# converter_tools/calc_tools/workflow_tools 已随 simple 死工具下线删除，
# 对应测试类随之退役——现役工具面由 simple/test_system_tools.py 覆盖。
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


# ── 外部 MCP plugin.json 校验 ──────────────────────────


class TestExternalMcpPluginJson:
    """验证外部 MCP 工具的 plugin.json 格式（web_search 已被 omnisearch 聚合替代删除）。"""

    EXTERNAL_DIR = Path(__file__).resolve().parent.parent / "external_mcp"

    @pytest.mark.parametrize("tool_name", [
        "browser_test", "design_generate",
        "design_review", "mcp_registry", "smithery", "resource_search",
    ])
    def test_external_mcp_plugin_json(self, tool_name):
        plugin_json_path = self.EXTERNAL_DIR / tool_name / "plugin.json"
        assert plugin_json_path.exists(), f"Missing plugin.json for {tool_name}"

        data = json.loads(plugin_json_path.read_text(encoding="utf-8"))
        assert data["entry"] == "mcp:external", f"{tool_name} should use mcp:external entry"
        assert "mcp" in data, f"{tool_name} should have mcp config"
        assert data["mcp"]["transport"] in ("stdio", "streamable_http"), (
            f"{tool_name} mcp.transport must be stdio or streamable_http"
        )
        assert "endpoint" in data["mcp"], f"{tool_name} mcp must have endpoint"
        assert "capabilities" in data
        assert len(data["capabilities"]["tools"]) >= 1


# ── 复杂工具 plugin.json + server.py 存在性校验 ──────────


class TestComplexToolsStructure:
    """验证 18 个复杂工具的目录结构。"""

    TOOLS_DIR = Path(__file__).resolve().parent.parent

    @pytest.mark.parametrize("tool_dir", [
        "bash", "download", "resource_merge",
        "task", "task_submit", "task_evaluate",
        "search", "triggers_ext",
        "media", "lsp", "memory",
        "human", "web_ext",
    ])
    def test_complex_tool_has_plugin_json(self, tool_dir):
        assert (self.TOOLS_DIR / tool_dir / "plugin.json").exists(), \
            f"Missing plugin.json for {tool_dir}"

    @pytest.mark.parametrize("tool_dir", [
        "bash", "download", "resource_merge",
        "task", "task_submit", "task_evaluate",
        "search", "triggers_ext",
        "media", "lsp", "memory",
        "human", "web_ext",
    ])
    def test_complex_tool_has_server_py(self, tool_dir):
        assert (self.TOOLS_DIR / tool_dir / "server.py").exists(), \
            f"Missing server.py for {tool_dir}"

    @pytest.mark.parametrize("tool_dir", [
        "bash", "download", "resource_merge",
        "task", "task_submit", "task_evaluate",
        "search", "triggers_ext",
        "media", "lsp", "memory",
        "human", "web_ext",
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
