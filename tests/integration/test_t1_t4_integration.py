"""T1~T4 组件集成测试。

验证四个组件之间的接口对齐、数据格式兼容和协同工作：
1. T1(capability_adapters.yaml) ↔ AdapterConfig 加载器
2. T2(ToolContextPlugin) ↔ capability_adapters.yaml
3. T2(ToolContextPlugin) ↔ Electron WindowInfo（通过桥接层）
4. tool_context ↔ T4(ApprovalViewRoutePlugin)
5. 边界场景：工具全部离线、Electron未启动、未知内容类型
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from connectors.adapter_config import (
    AdapterConfig,
    get_adapter_status_summary,
    load_adapter_configs,
)
from bridge.window_info import (
    WindowInfoData,
    normalize_window_info,
    validate_window_info,
)
from pipeline.plugin import PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys
from plugins.input.tool_context import ToolContextPlugin
from plugins.output.approval_view_route import ApprovalViewRoutePlugin
from plugins.output.approval_view_route.plugin import RenderMode

# 从 conftest 导入辅助函数
from tests.integration.conftest import make_ctx, make_mock_registry


# ═══════════════════════════════════════════════════════════
# 集成点 1：T1(capability_adapters.yaml) ↔ AdapterConfig
# ═══════════════════════════════════════════════════════════


class TestAdapterConfigLoad:
    """验证 capability_adapters.yaml 能被正确加载和解析。"""

    def test_load_adapters_from_yaml(self, adapter_config_path: Any) -> None:
        """应成功加载四个适配器配置。"""
        configs = load_adapter_configs(adapter_config_path)
        assert len(configs) == 4
        assert "vscode" in configs
        assert "comfyui" in configs
        assert "playwright" in configs
        assert "windows_desktop" in configs

    def test_vscode_adapter_fields(self, adapter_config_path: Any) -> None:
        """VSCode 适配器字段应与 YAML 一致。"""
        configs = load_adapter_configs(adapter_config_path)
        vscode = configs["vscode"]

        assert vscode.name == "vscode"
        assert vscode.adapter_type == "ide"
        assert vscode.priority == 10
        assert vscode.display_name == "Visual Studio Code"
        assert vscode.available is True
        assert vscode.has_mcp is False
        assert vscode.connector_class == "connectors.vscode.connector.VSCodeConnector"
        assert "open_file" in vscode.capabilities
        assert "show_diff" in vscode.capabilities

    def test_comfyui_adapter_fields(self, adapter_config_path: Any) -> None:
        """ComfyUI 适配器字段应正确。"""
        configs = load_adapter_configs(adapter_config_path)
        comfyui = configs["comfyui"]

        assert comfyui.adapter_type == "creative"
        assert comfyui.has_mcp is False
        assert "generate_image" in comfyui.capabilities

    def test_playwright_adapter_has_mcp(self, adapter_config_path: Any) -> None:
        """Playwright 适配器应标记为 MCP 连接器。"""
        configs = load_adapter_configs(adapter_config_path)
        playwright = configs["playwright"]

        assert playwright.adapter_type == "browser"
        assert playwright.has_mcp is True
        assert playwright.connector_class is None
        assert "screenshot" in playwright.capabilities

    def test_windows_desktop_adapter_has_mcp(self, adapter_config_path: Any) -> None:
        """Windows Desktop 适配器应标记为 MCP 连接器。"""
        configs = load_adapter_configs(adapter_config_path)
        desktop = configs["windows_desktop"]

        assert desktop.adapter_type == "desktop"
        assert desktop.has_mcp is True
        assert "screen_capture" in desktop.capabilities
        assert "clipboard_read" in desktop.capabilities

    def test_adapter_status_summary(self, adapter_config_path: Any) -> None:
        """get_adapter_status_summary 应返回四个适配器的状态摘要。"""
        configs = load_adapter_configs(adapter_config_path)
        summary = get_adapter_status_summary(configs)

        assert len(summary) == 4
        assert summary["vscode"]["type"] == "ide"
        assert summary["vscode"]["available"] is True
        assert summary["vscode"]["has_mcp"] is False
        assert summary["vscode"]["capabilities_count"] == 6

        assert summary["playwright"]["has_mcp"] is True
        assert summary["playwright"]["capabilities_count"] == 5

    def test_load_nonexistent_path_returns_empty(self) -> None:
        """配置文件不存在时应返回空字典。"""
        configs = load_adapter_configs("/nonexistent/path.yaml")
        assert configs == {}


# ═══════════════════════════════════════════════════════════
# 集成点 2：T2(ToolContextPlugin) ↔ capability_adapters.yaml
# ═══════════════════════════════════════════════════════════


class TestToolContextWithAdapters:
    """验证 ToolContextPlugin 能正确读取适配器配置。"""

    @pytest.mark.asyncio
    async def test_tool_context_includes_adapter_status(
        self, adapter_config_path: Any
    ) -> None:
        """tool_context 应包含 adapter_status 字段。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        ctx = make_ctx(
            state={},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        assert "adapter_status" in tool_ctx
        assert isinstance(tool_ctx["adapter_status"], dict)
        # 应包含四个适配器
        assert len(tool_ctx["adapter_status"]) == 4
        assert "vscode" in tool_ctx["adapter_status"]
        assert "comfyui" in tool_ctx["adapter_status"]
        assert "playwright" in tool_ctx["adapter_status"]
        assert "windows_desktop" in tool_ctx["adapter_status"]

    @pytest.mark.asyncio
    async def test_adapter_status_has_required_fields(
        self, adapter_config_path: Any
    ) -> None:
        """每个适配器状态应包含 type/available/capabilities_count/has_mcp。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        ctx = make_ctx(
            state={},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        adapter_status = result.state_updates["tool_context"]["adapter_status"]

        for name, status in adapter_status.items():
            assert "type" in status, f"{name} 缺少 type 字段"
            assert "available" in status, f"{name} 缺少 available 字段"
            assert "capabilities_count" in status, f"{name} 缺少 capabilities_count"
            assert "has_mcp" in status, f"{name} 缺少 has_mcp 字段"
            assert "display_name" in status, f"{name} 缺少 display_name 字段"


# ═══════════════════════════════════════════════════════════
# 集成点 3：Electron WindowInfo ↔ ToolContextPlugin（桥接层）
# ═══════════════════════════════════════════════════════════


class TestWindowInfoBridge:
    """验证 Electron 窗口信息数据格式对齐。"""

    def test_standard_format_normalization(
        self, electron_window_standard: Any
    ) -> None:
        """Electron 标准格式应被正确规范化。"""
        result = normalize_window_info(electron_window_standard)

        assert result is not None
        assert isinstance(result, WindowInfoData)
        assert result.title == "test.py - Visual Studio Code"
        assert result.processName == "Code"
        assert result.x == 100
        assert result.y == 50
        assert result.width == 1920
        assert result.height == 1080
        assert result.is_valid

    def test_standard_format_to_dict(
        self, electron_window_standard: Any
    ) -> None:
        """规范化后 to_dict 应输出与 Electron 对齐的字段名。"""
        result = normalize_window_info(electron_window_standard)
        assert result is not None

        d = result.to_dict()
        # 字段名与 Electron WindowInfo 接口一致
        assert "title" in d
        assert "processName" in d
        assert "x" in d
        assert "y" in d
        assert "width" in d
        assert "height" in d
        assert d["processName"] == "Code"

    def test_legacy_format_compatibility(
        self, electron_window_legacy: Any
    ) -> None:
        """旧格式（app/bounds）应被兼容解析。"""
        result = normalize_window_info(electron_window_legacy)

        assert result is not None
        assert result.processName == "VSCode"  # app -> processName
        assert result.x == 0
        assert result.width == 800

    def test_empty_dict_returns_none(self) -> None:
        """空字典应返回 None。"""
        assert normalize_window_info({}) is None

    def test_non_dict_returns_none(self) -> None:
        """非字典输入应返回 None。"""
        assert normalize_window_info("not a dict") is None
        assert normalize_window_info(42) is None
        assert normalize_window_info(None) is None

    def test_validate_valid_window(self) -> None:
        """有效窗口信息校验应通过。"""
        data = WindowInfoData(title="test", processName="Code")
        issues = validate_window_info(data)
        assert issues == []

    def test_validate_empty_window(self) -> None:
        """空窗口信息校验应报告问题。"""
        data = WindowInfoData()
        issues = validate_window_info(data)
        assert len(issues) > 0

    @pytest.mark.asyncio
    async def test_plugin_consumes_standard_window_info(
        self, electron_window_standard: Any
    ) -> None:
        """ToolContextPlugin 应能消费标准 Electron 窗口信息。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        ctx = make_ctx(
            state={"electron_window": electron_window_standard},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        active_window = result.state_updates["tool_context"]["active_window"]

        assert active_window is not None
        assert active_window["title"] == "test.py - Visual Studio Code"
        assert active_window["processName"] == "Code"
        assert active_window["width"] == 1920

    @pytest.mark.asyncio
    async def test_plugin_consumes_legacy_window_info(
        self, electron_window_legacy: Any
    ) -> None:
        """ToolContextPlugin 应兼容旧格式窗口信息。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        ctx = make_ctx(
            state={"electron_window": electron_window_legacy},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        active_window = result.state_updates["tool_context"]["active_window"]

        assert active_window is not None
        assert active_window["processName"] == "VSCode"  # app -> processName
        assert active_window["width"] == 800

    @pytest.mark.asyncio
    async def test_no_electron_window_returns_none(self) -> None:
        """Electron 未启动时 active_window 应为 None。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        ctx = make_ctx(
            state={},  # 无 electron_window
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates["tool_context"]["active_window"] is None


# ═══════════════════════════════════════════════════════════
# 集成点 4：tool_context ↔ T4(ApprovalViewRoutePlugin)
# ═══════════════════════════════════════════════════════════


class TestApprovalViewRouteIntegration:
    """验证 tool_context 数据结构满足审批视图路由的输入要求。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_text_approval(
        self, adapter_config_path: Any
    ) -> None:
        """端到端：文本审批应路由到 text 渲染模式。"""
        # 1. ToolContextPlugin 构建 tool_context
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        state: dict[str, Any] = {
            "content_type": "text",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {"content": "hello"},
        }
        ctx = make_ctx(state=state, services={"tool_registry": registry})

        tc_result = await tc_plugin.execute(ctx)
        # 模拟状态合并
        state.update(tc_result.state_updates)

        # 2. ApprovalViewRoutePlugin 消费 tool_context
        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates.get("approval_render_mode") == "text"
        assert "text" in av_result.state_updates.get(StateKeys.ROUTED_TO, "")

    @pytest.mark.asyncio
    async def test_full_pipeline_code_diff_approval(
        self, adapter_config_path: Any
    ) -> None:
        """端到端：代码差异应路由到 code_diff 渲染模式。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["file_read"])
        state: dict[str, Any] = {
            "content_type": "code_diff",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {"diff": "+new line"},
        }
        ctx = make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates.get("approval_render_mode") == "code_diff"

    @pytest.mark.asyncio
    async def test_full_pipeline_command_approval(
        self, adapter_config_path: Any
    ) -> None:
        """端到端：命令确认应路由到 command 渲染模式。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        state: dict[str, Any] = {
            "content_type": "command",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {"command": "rm -rf /tmp/test"},
        }
        ctx = make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates.get("approval_render_mode") == "command"

    @pytest.mark.asyncio
    async def test_no_approval_required_skips_route(
        self, adapter_config_path: Any
    ) -> None:
        """无需审批时路由插件应跳过。"""
        av_plugin = ApprovalViewRoutePlugin(config={})
        state: dict[str, Any] = {
            StateKeys.APPROVAL_REQUIRED: False,
        }
        ctx = make_ctx(state=state, services={})

        result = await av_plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_tool_context_has_all_required_fields_for_route(
        self, adapter_config_path: Any
    ) -> None:
        """tool_context 应包含审批路由所需的所有字段。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        ctx = make_ctx(
            state={},
            services={"tool_registry": registry},
        )

        result = await tc_plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        # 验证 tool_context 满足审批路由输入要求
        required_fields = {"online_tools", "active_window", "adapter_status", "timestamp"}
        assert required_fields.issubset(set(tool_ctx.keys()))


# ═══════════════════════════════════════════════════════════
# 集成点 5：边界场景
# ═══════════════════════════════════════════════════════════


class TestEdgeCasesIntegration:
    """边界场景集成测试。"""

    @pytest.mark.asyncio
    async def test_all_tools_offline(self, adapter_config_path: Any) -> None:
        """工具全部离线时不应崩溃，返回空 online_tools。"""
        plugin = ToolContextPlugin(config={})
        # 所有工具没有 handler
        registry = make_mock_registry(
            tool_names=["tool_a", "tool_b"],
            has_handlers={"tool_a": False, "tool_b": False},
        )
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        assert tool_ctx["online_tools"] == []
        assert tool_ctx["adapter_status"]  # 适配器配置仍可用
        assert tool_ctx["timestamp"] > 0

    @pytest.mark.asyncio
    async def test_electron_not_started(self, adapter_config_path: Any) -> None:
        """Electron 未启动时不应崩溃，active_window 为 None。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        ctx = make_ctx(
            state={},  # 无 electron_window
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        assert tool_ctx["active_window"] is None
        assert tool_ctx["online_tools"] == ["bash_execute"]

    @pytest.mark.asyncio
    async def test_unknown_content_type_routes_to_unknown(
        self, adapter_config_path: Any
    ) -> None:
        """未知内容类型应路由到 unknown 渲染模式。"""
        av_plugin = ApprovalViewRoutePlugin(config={})
        state: dict[str, Any] = {
            "content_type": "custom_3d_render",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = make_ctx(state=state, services={})

        result = await av_plugin.execute(ctx)
        assert result.state_updates.get("approval_render_mode") == "unknown"

    @pytest.mark.asyncio
    async def test_registry_unavailable_graceful_degradation(
        self, adapter_config_path: Any
    ) -> None:
        """ToolRegistry 不可用时 ToolContextPlugin 应优雅降级。"""
        plugin = ToolContextPlugin(config={})
        ctx = make_ctx(state={}, services={})  # 无 tool_registry

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        assert tool_ctx["online_tools"] == []
        assert tool_ctx["active_window"] is None
        assert tool_ctx["adapter_status"]  # 适配器配置独立于 ToolRegistry
        assert tool_ctx["timestamp"] > 0

    @pytest.mark.asyncio
    async def test_full_pipeline_with_electron_window(
        self, adapter_config_path: Any, electron_window_standard: Any
    ) -> None:
        """端到端：含 Electron 窗口信息的完整流程。"""
        # 1. ToolContextPlugin
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["file_read", "file_write"])
        state: dict[str, Any] = {
            "electron_window": electron_window_standard,
            "content_type": "file_change",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {"content": "file changed"},
        }
        ctx = make_ctx(state=state, services={"tool_registry": registry})

        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        # 验证 tool_context 完整性
        tool_ctx = state["tool_context"]
        assert len(tool_ctx["online_tools"]) == 2
        assert tool_ctx["active_window"]["processName"] == "Code"
        assert "vscode" in tool_ctx["adapter_status"]

        # 2. ApprovalViewRoutePlugin
        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates.get("approval_render_mode") == "file_change"

        # 3. 验证适配器匹配（file_change 需要 open_file 能力）
        routed_to = av_result.state_updates.get(StateKeys.ROUTED_TO, "")
        assert "file_change" in routed_to

    def test_adapter_config_path_exists(self, adapter_config_path: Any) -> None:
        """配置文件应存在。"""
        assert adapter_config_path.exists(), (
            f"配置文件不存在: {adapter_config_path}"
        )

    def test_all_four_adapters_present(self, adapter_config_path: Any) -> None:
        """配置文件应包含全部四个适配器。"""
        configs = load_adapter_configs(adapter_config_path)
        expected = {"vscode", "comfyui", "playwright", "windows_desktop"}
        actual = set(configs.keys())
        assert actual == expected, f"缺少适配器: {expected - actual}"
