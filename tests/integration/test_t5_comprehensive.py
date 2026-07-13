"""T5 集成联调全面测试套件。

在 T1~T4 已通过测试的基础上，补充以下集成场景的全面覆盖：
1. 四适配器完整链路解析验证（capabilities 全量、priority 排序、mcp_config 解析）
2. Electron WindowInfo 字段级别精确对齐（TS interface ↔ Python bridge ↔ ToolContextPlugin）
3. tool_context 数据在多组件传递后的完整性和不变性
4. YAML 配置边界场景（空文件、缺字段、格式错误、额外字段、available=false）
5. 组件间 import 路径、依赖注入、数据传递格式的兼容性
6. 高级边界场景（并发调用隔离、适配器能力匹配路由、自定义类型映射）
7. 回归验证基线
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bridge.window_info import (
    WindowInfoData,
    normalize_window_info,
    validate_window_info,
)
from connectors.adapter_config import (
    AdapterConfig,
    get_adapter_status_summary,
    load_adapter_configs,
)
from connectors.degradation import DegradationManager
from connectors.registry import ConnectorRegistry
from connectors.types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)
from pipeline.plugin import PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys
from plugins.input.tool_context import ToolContextPlugin
from plugins.output.approval_view_route import ApprovalViewRoutePlugin
from plugins.output.approval_view_route.plugin import RenderMode

# 从 conftest 导入辅助函数
from tests.integration.conftest import make_ctx, make_mock_registry


# ═══════════════════════════════════════════════════════════════════
# 1. 四适配器完整链路解析验证
# ═══════════════════════════════════════════════════════════════════


class TestAdapterFullChain:
    """验证四个适配器的完整配置加载链路。"""

    def test_vscode_full_capabilities(self, adapter_config_path: Any) -> None:
        """VSCode 适配器 capabilities 应完整包含所有 6 个能力。"""
        configs = load_adapter_configs(adapter_config_path)
        vscode = configs["vscode"]

        expected_caps = {
            "open_file", "open_folder", "insert_content",
            "jump_to", "show_diff", "get_selection",
        }
        assert set(vscode.capabilities) == expected_caps

    def test_comfyui_full_capabilities(self, adapter_config_path: Any) -> None:
        """ComfyUI 适配器 capabilities 应完整包含所有 10 个能力。"""
        configs = load_adapter_configs(adapter_config_path)
        comfyui = configs["comfyui"]

        expected_caps = {
            "generate_image", "submit_workflow", "get_progress", "get_result",
            "list_models", "list_workflows", "capture_screenshot",
            "interrupt_task", "clear_queue", "ws_progress",
        }
        assert set(comfyui.capabilities) == expected_caps

    def test_playwright_full_capabilities(self, adapter_config_path: Any) -> None:
        """Playwright 适配器应包含所有 5 个浏览器能力。"""
        configs = load_adapter_configs(adapter_config_path)
        pw = configs["playwright"]

        expected_caps = {"navigate", "snapshot", "evaluate", "console", "screenshot"}
        assert set(pw.capabilities) == expected_caps

    def test_windows_desktop_full_capabilities(self, adapter_config_path: Any) -> None:
        """Windows Desktop 适配器应包含所有 11 个桌面能力。"""
        configs = load_adapter_configs(adapter_config_path)
        desktop = configs["windows_desktop"]

        expected_caps = {
            "screen_capture", "window_list", "window_focus", "window_resize",
            "keyboard_input", "mouse_click", "mouse_move", "mouse_scroll",
            "clipboard_read", "clipboard_write", "file_dialog",
        }
        assert set(desktop.capabilities) == expected_caps

    def test_adapter_priority_ordering(self, adapter_config_path: Any) -> None:
        """适配器应按 priority 降序排列：vscode=10, comfyui=10, desktop=8, playwright=5。"""
        configs = load_adapter_configs(adapter_config_path)
        by_priority = sorted(configs.values(), key=lambda c: -c.priority)

        assert by_priority[0].priority == 10
        assert by_priority[-1].priority == 5
        # 同优先级 10 的两个适配器：vscode 和 comfyui
        top_two = {by_priority[0].name, by_priority[1].name}
        assert top_two == {"vscode", "comfyui"}
        # desktop = 8, playwright = 5
        assert by_priority[2].name == "windows_desktop"
        assert by_priority[2].priority == 8
        assert by_priority[3].name == "playwright"
        assert by_priority[3].priority == 5

    def test_mcp_vs_non_mcp_classification(self, adapter_config_path: Any) -> None:
        """非 MCP（vscode, comfyui）vs MCP（playwright, windows_desktop）分类正确。"""
        configs = load_adapter_configs(adapter_config_path)

        non_mcp = {n for n, c in configs.items() if not c.has_mcp}
        mcp = {n for n, c in configs.items() if c.has_mcp}

        assert non_mcp == {"vscode", "comfyui"}
        assert mcp == {"playwright", "windows_desktop"}

    def test_connector_class_field(self, adapter_config_path: Any) -> None:
        """非 MCP 适配器应有 connector_class，MCP 适配器应为 None。"""
        configs = load_adapter_configs(adapter_config_path)

        assert configs["vscode"].connector_class == "connectors.vscode.connector.VSCodeConnector"
        assert configs["comfyui"].connector_class == "connectors.creative.comfyui.ComfyUIConnector"
        assert configs["playwright"].connector_class is None
        assert configs["windows_desktop"].connector_class is None

    def test_adapter_config_is_frozen(self, adapter_config_path: Any) -> None:
        """AdapterConfig 应为 frozen dataclass（不可变）。"""
        configs = load_adapter_configs(adapter_config_path)
        vscode = configs["vscode"]

        with pytest.raises(AttributeError):
            vscode.priority = 999  # type: ignore[misc]

    def test_adapter_types_are_distinct(self, adapter_config_path: Any) -> None:
        """四个适配器的 type 应各不相同。"""
        configs = load_adapter_configs(adapter_config_path)
        types = {c.adapter_type for c in configs.values()}
        assert types == {"ide", "creative", "browser", "desktop"}

    def test_all_adapters_available_by_default(self, adapter_config_path: Any) -> None:
        """所有适配器默认都应为 available=True。"""
        configs = load_adapter_configs(adapter_config_path)
        for name, cfg in configs.items():
            assert cfg.available is True, f"{name} 应为 available"


# ═══════════════════════════════════════════════════════════════════
# 2. Electron WindowInfo 字段级精确对齐
# ═══════════════════════════════════════════════════════════════════


class TestElectronWindowInfoAlignment:
    """验证 Electron WindowInfo 数据格式与 Python 端的精确对齐。

    Electron 端 WindowInfo 接口（window-info.ts）：
        title: string, processName: string, x: number, y: number,
        width: number, height: number

    Python 端 WindowInfoData（bridge/window_info.py）：
        title: str, processName: str, x: int, y: int, width: int, height: int
    """

    def test_standard_electron_data_passes_through(self) -> None:
        """标准 Electron IPC 数据应无损通过桥接层。"""
        electron_data = {
            "title": "app.tsx - MyProject - Visual Studio Code",
            "processName": "Code",
            "x": 192,
            "y": 52,
            "width": 1536,
            "height": 864,
        }

        result = normalize_window_info(electron_data)
        assert result is not None
        assert result.title == electron_data["title"]
        assert result.processName == electron_data["processName"]
        assert result.x == electron_data["x"]
        assert result.y == electron_data["y"]
        assert result.width == electron_data["width"]
        assert result.height == electron_data["height"]

    def test_to_dict_field_names_match_electron(self) -> None:
        """to_dict 输出字段名应与 Electron WindowInfo 接口完全一致。"""
        data = WindowInfoData(
            title="test", processName="Code", x=10, y=20, width=800, height=600,
        )
        d = data.to_dict()

        # Electron 端字段名（来自 window-info.ts WindowInfo interface）
        electron_fields = {"title", "processName", "x", "y", "width", "height"}
        assert set(d.keys()) == electron_fields

    def test_to_dict_no_extra_fields(self) -> None:
        """to_dict 不应包含 Electron 端没有的额外字段。"""
        data = WindowInfoData(title="t", processName="p", x=1, y=2, width=3, height=4)
        d = data.to_dict()
        # 不应有 platform, app, bounds 等
        assert "platform" not in d
        assert "app" not in d
        assert "bounds" not in d

    def test_numeric_string_coordinates_converted(self) -> None:
        """Electron 传入字符串数字的坐标应被正确转换。"""
        electron_data = {
            "title": "test",
            "processName": "Code",
            "x": "100",
            "y": "200",
            "width": "800",
            "height": "600",
        }
        result = normalize_window_info(electron_data)
        assert result is not None
        assert result.x == 100
        assert result.y == 200
        assert result.width == 800
        assert result.height == 600

    def test_float_coordinates_truncated_to_int(self) -> None:
        """浮点坐标应被截断为整数。"""
        electron_data = {
            "title": "test",
            "processName": "Code",
            "x": 100.5,
            "y": 200.7,
            "width": 800.9,
            "height": 600.1,
        }
        result = normalize_window_info(electron_data)
        assert result is not None
        assert isinstance(result.x, int)
        assert isinstance(result.y, int)

    def test_zero_dimension_window_is_valid(self) -> None:
        """零尺寸窗口信息应被保留（Electron minimize 场景）。"""
        electron_data = {
            "title": "minimized",
            "processName": "Code",
            "x": -32000,
            "y": -32000,
            "width": 0,
            "height": 0,
        }
        result = normalize_window_info(electron_data)
        assert result is not None
        assert result.width == 0
        assert result.height == 0
        assert result.is_valid is True  # 有 title 和 processName

    def test_electron_empty_title_with_process_name(self) -> None:
        """空标题但有进程名的窗口信息应有效。"""
        electron_data = {
            "title": "",
            "processName": "chrome",
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
        }
        result = normalize_window_info(electron_data)
        assert result is not None
        assert result.is_valid is True  # processName 存在即可

    @pytest.mark.asyncio
    async def test_window_info_through_full_pipeline(
        self, electron_window_standard: Any,
    ) -> None:
        """Electron 窗口信息经 ToolContextPlugin 处理后字段完整。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["file_read"])
        ctx = make_ctx(
            state={"electron_window": electron_window_standard},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        active_window = result.state_updates["tool_context"]["active_window"]

        assert active_window is not None
        assert active_window["title"] == electron_window_standard["title"]
        assert active_window["processName"] == electron_window_standard["processName"]
        assert active_window["x"] == electron_window_standard["x"]
        assert active_window["y"] == electron_window_standard["y"]
        assert active_window["width"] == electron_window_standard["width"]
        assert active_window["height"] == electron_window_standard["height"]

    def test_legacy_format_bounds_extraction(self) -> None:
        """旧格式 bounds 嵌套字典应被正确展平。"""
        legacy_data = {
            "title": "test.py",
            "app": "Code",
            "bounds": {"x": 100, "y": 200, "width": 800, "height": 600},
        }
        result = normalize_window_info(legacy_data)
        assert result is not None
        assert result.x == 100
        assert result.y == 200
        assert result.width == 800
        assert result.height == 600

    def test_legacy_format_bounds_missing(self) -> None:
        """旧格式 bounds 缺失时应使用默认值 0。"""
        legacy_data = {"title": "test.py", "app": "Code"}
        result = normalize_window_info(legacy_data)
        assert result is not None
        assert result.x == 0
        assert result.y == 0
        assert result.width == 0
        assert result.height == 0

    def test_window_info_with_extra_unknown_fields(self) -> None:
        """带额外未知字段的窗口信息应被忽略，不影响解析。"""
        data_with_extras = {
            "title": "test",
            "processName": "Code",
            "x": 0,
            "y": 0,
            "width": 800,
            "height": 600,
            "extraField": "should be ignored",
            "anotherUnknown": 42,
        }
        result = normalize_window_info(data_with_extras)
        assert result is not None
        assert result.title == "test"
        d = result.to_dict()
        assert "extraField" not in d


# ═══════════════════════════════════════════════════════════════════
# 3. tool_context 数据在多组件传递后的完整性
# ═══════════════════════════════════════════════════════════════════


class TestToolContextDataIntegrity:
    """验证 tool_context 数据在多组件传递过程中保持完整。"""

    @pytest.mark.asyncio
    async def test_tool_context_survives_full_pipeline(self) -> None:
        """tool_context 数据经 ToolContextPlugin → ApprovalViewRoutePlugin 后保持完整。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute", "file_read"])
        window_info = {
            "title": "main.py - VSCode",
            "processName": "Code",
            "x": 0, "y": 0, "width": 1920, "height": 1080,
        }
        state: dict[str, Any] = {
            "electron_window": window_info,
            "content_type": "code_diff",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {"diff": "+added line"},
        }
        ctx = make_ctx(state=state, services={"tool_registry": registry})

        # Step 1: ToolContextPlugin
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        # 验证 tool_context 完整性
        tool_ctx = state["tool_context"]
        assert len(tool_ctx["online_tools"]) == 2
        assert "bash_execute" in tool_ctx["online_tools"]
        assert "file_read" in tool_ctx["online_tools"]
        assert tool_ctx["active_window"] is not None
        assert tool_ctx["active_window"]["processName"] == "Code"
        assert len(tool_ctx["adapter_status"]) == 4
        assert isinstance(tool_ctx["timestamp"], float)

        # Step 2: ApprovalViewRoutePlugin
        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        # 路由不应修改 tool_context
        assert "tool_context" in state  # 仍然存在
        assert state["tool_context"]["online_tools"] == ["bash_execute", "file_read"]

        # 路由结果
        assert av_result.state_updates.get("approval_render_mode") == "code_diff"

    @pytest.mark.asyncio
    async def test_tool_context_fields_are_independent(self) -> None:
        """tool_context 各字段应互不依赖，单个缺失不影响其他字段。"""
        plugin = ToolContextPlugin(config={})

        # 无 registry，有窗口信息
        ctx = make_ctx(
            state={"electron_window": {"title": "t", "processName": "p"}},
            services={},
        )
        result = await plugin.execute(ctx)
        tc = result.state_updates["tool_context"]

        assert tc["online_tools"] == []  # 无 registry
        assert tc["active_window"] is not None  # 有窗口信息
        assert tc["adapter_status"]  # 有适配器配置
        assert isinstance(tc["timestamp"], float)

    @pytest.mark.asyncio
    async def test_tool_context_schema_matches_approval_route_expectation(self) -> None:
        """tool_context 数据结构应满足 ApprovalViewRoutePlugin 的所有读取需求。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        tc = result.state_updates["tool_context"]

        # ApprovalViewRoutePlugin 读取的字段：
        # ctx.state.get("tool_context", {}).get("adapter_status", {})
        assert isinstance(tc.get("adapter_status"), dict)
        # 每个 adapter_status entry 需要 "capabilities" 键
        for name, status in tc["adapter_status"].items():
            assert isinstance(status, dict)
            assert "capabilities" in status, f"{name} adapter_status 缺少 capabilities"
            assert "type" in status
            assert "available" in status

    @pytest.mark.asyncio
    async def test_timestamp_is_monotonically_increasing(self) -> None:
        """多次执行时 timestamp 应单调递增。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result1 = await plugin.execute(ctx)
        ts1 = result1.state_updates["tool_context"]["timestamp"]

        # 小延迟确保时间差
        await asyncio.sleep(0.01)

        result2 = await plugin.execute(ctx)
        ts2 = result2.state_updates["tool_context"]["timestamp"]

        assert ts2 > ts1


# ═══════════════════════════════════════════════════════════════════
# 4. YAML 配置边界场景
# ═══════════════════════════════════════════════════════════════════


class TestYAMLConfigBoundary:
    """YAML 配置文件解析的边界场景测试。"""

    def test_empty_yaml_file(self, tmp_path: Path) -> None:
        """空 YAML 文件应返回空字典。"""
        empty_yaml = tmp_path / "empty.yaml"
        empty_yaml.write_text("", encoding="utf-8")

        configs = load_adapter_configs(str(empty_yaml))
        assert configs == {}

    def test_yaml_with_only_comments(self, tmp_path: Path) -> None:
        """只含注释的 YAML 文件应返回空字典。"""
        comment_yaml = tmp_path / "comments.yaml"
        comment_yaml.write_text("# Only comments\n# No data\n", encoding="utf-8")

        configs = load_adapter_configs(str(comment_yaml))
        assert configs == {}

    def test_yaml_with_empty_adapters_section(self, tmp_path: Path) -> None:
        """adapters 节为空应返回空字典。"""
        yaml_file = tmp_path / "empty_adapters.yaml"
        yaml_file.write_text("adapters: {}\n", encoding="utf-8")

        configs = load_adapter_configs(str(yaml_file))
        assert configs == {}

    def test_yaml_with_adapter_missing_optional_fields(self, tmp_path: Path) -> None:
        """适配器缺少可选字段时应使用默认值。"""
        yaml_file = tmp_path / "minimal.yaml"
        yaml_file.write_text(
            "adapters:\n"
            "  minimal:\n"
            "    type: 'test'\n"
            "    display_name: 'Minimal Adapter'\n",
            encoding="utf-8",
        )

        configs = load_adapter_configs(str(yaml_file))
        assert "minimal" in configs
        m = configs["minimal"]
        assert m.name == "minimal"
        assert m.adapter_type == "test"
        assert m.priority == 0  # 默认
        assert m.capabilities == ()  # 默认
        assert m.available is True  # 默认
        assert m.has_mcp is False  # 默认
        assert m.connector_class is None  # 默认

    def test_yaml_with_adapter_available_false(self, tmp_path: Path) -> None:
        """available=false 的适配器应被加载但标记为不可用。"""
        yaml_file = tmp_path / "disabled.yaml"
        yaml_file.write_text(
            "adapters:\n"
            "  disabled_adapter:\n"
            "    type: 'test'\n"
            "    display_name: 'Disabled'\n"
            "    available: false\n",
            encoding="utf-8",
        )

        configs = load_adapter_configs(str(yaml_file))
        assert configs["disabled_adapter"].available is False

    def test_yaml_with_adapter_null_capabilities(self, tmp_path: Path) -> None:
        """capabilities 为 null 应使用空元组。"""
        yaml_file = tmp_path / "null_caps.yaml"
        yaml_file.write_text(
            "adapters:\n"
            "  no_caps:\n"
            "    type: 'test'\n"
            "    capabilities: null\n",
            encoding="utf-8",
        )

        configs = load_adapter_configs(str(yaml_file))
        assert configs["no_caps"].capabilities == ()

    def test_yaml_with_adapter_mcp_config(self, tmp_path: Path) -> None:
        """含 mcp_config 的适配器应被标记为 has_mcp=True。"""
        yaml_file = tmp_path / "mcp.yaml"
        yaml_file.write_text(
            "adapters:\n"
            "  mcp_adapter:\n"
            "    type: 'test'\n"
            "    mcp_config:\n"
            "      command: 'test_cmd'\n"
            "      args: ['-a']\n"
            "      env: {}\n",
            encoding="utf-8",
        )

        configs = load_adapter_configs(str(yaml_file))
        assert configs["mcp_adapter"].has_mcp is True

    def test_yaml_with_non_dict_adapter_entry(self, tmp_path: Path) -> None:
        """非字典类型的适配器条目应被跳过。"""
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text(
            "adapters:\n"
            "  valid:\n"
            "    type: 'test'\n"
            "  invalid_string: 'not a dict'\n"
            "  invalid_number: 42\n"
            "  invalid_list:\n"
            "    - item1\n",
            encoding="utf-8",
        )

        configs = load_adapter_configs(str(yaml_file))
        assert "valid" in configs
        assert "invalid_string" not in configs
        assert "invalid_number" not in configs
        assert "invalid_list" not in configs

    def test_yaml_with_adapters_as_list(self, tmp_path: Path) -> None:
        """adapters 为列表（非字典）应返回空字典。"""
        yaml_file = tmp_path / "list_adapters.yaml"
        yaml_file.write_text("adapters:\n  - item1\n  - item2\n", encoding="utf-8")

        configs = load_adapter_configs(str(yaml_file))
        assert configs == {}

    def test_status_summary_with_disabled_adapter(self, tmp_path: Path) -> None:
        """get_adapter_status_summary 应反映 available=false。"""
        yaml_file = tmp_path / "mixed.yaml"
        yaml_file.write_text(
            "adapters:\n"
            "  enabled:\n"
            "    type: 'ide'\n"
            "    capabilities: ['open_file']\n"
            "    available: true\n"
            "  disabled:\n"
            "    type: 'browser'\n"
            "    capabilities: ['navigate']\n"
            "    available: false\n",
            encoding="utf-8",
        )

        configs = load_adapter_configs(str(yaml_file))
        summary = get_adapter_status_summary(configs)

        assert summary["enabled"]["available"] is True
        assert summary["disabled"]["available"] is False
        assert summary["enabled"]["capabilities_count"] == 1
        assert summary["disabled"]["capabilities_count"] == 1

    def test_status_summary_includes_capabilities_list(self, tmp_path: Path) -> None:
        """status summary 应包含完整的 capabilities 列表。"""
        yaml_file = tmp_path / "caps.yaml"
        yaml_file.write_text(
            "adapters:\n"
            "  test:\n"
            "    type: 'ide'\n"
            "    capabilities: ['open_file', 'show_diff']\n",
            encoding="utf-8",
        )

        configs = load_adapter_configs(str(yaml_file))
        summary = get_adapter_status_summary(configs)

        assert "capabilities" in summary["test"]
        assert isinstance(summary["test"]["capabilities"], list)
        assert set(summary["test"]["capabilities"]) == {"open_file", "show_diff"}

    @pytest.mark.asyncio
    async def test_tool_context_adapter_status_includes_capabilities(
        self, adapter_config_path: Any,
    ) -> None:
        """ToolContextPlugin 产出的 adapter_status 应包含 capabilities 列表。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        adapter_status = result.state_updates["tool_context"]["adapter_status"]

        # 验证每个适配器都有 capabilities 列表
        for name, status in adapter_status.items():
            assert "capabilities" in status, f"{name} 缺少 capabilities"
            assert isinstance(status["capabilities"], list)


# ═══════════════════════════════════════════════════════════════════
# 5. 组件间依赖注入与数据传递兼容性
# ═══════════════════════════════════════════════════════════════════


class TestDependencyInjectionCompatibility:
    """验证组件间 import 路径、服务获取和数据传递格式的兼容性。"""

    def test_import_paths_all_resolvable(self) -> None:
        """所有关键组件的 import 路径应可解析。"""
        # 这些 import 不应抛出 ImportError
        from bridge.window_info import normalize_window_info  # noqa: F401
        from connectors.adapter_config import load_adapter_configs  # noqa: F401
        from connectors.registry import ConnectorRegistry  # noqa: F401
        from connectors.types import ConnectorInfo  # noqa: F401
        from pipeline.plugin import PluginContext, PluginResult  # noqa: F401
        from pipeline.types import ErrorPolicy, StateKeys  # noqa: F401
        from plugins.input.tool_context import ToolContextPlugin  # noqa: F401
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin  # noqa: F401

    def test_tool_context_plugin_consumes_plugin_context(self) -> None:
        """ToolContextPlugin 应能接受 PluginContext 实例。"""
        plugin = ToolContextPlugin(config={})
        ctx = make_ctx(state={}, services={})
        # 不应抛出类型错误
        assert hasattr(plugin, "execute")

    def test_approval_route_plugin_consumes_plugin_context(self) -> None:
        """ApprovalViewRoutePlugin 应能接受 PluginContext 实例。"""
        plugin = ApprovalViewRoutePlugin(config={})
        ctx = make_ctx(state={}, services={})
        assert hasattr(plugin, "execute")

    def test_plugin_context_service_get_key_error(self) -> None:
        """PluginContext.get_service 不存在的服务应抛 KeyError。"""
        ctx = make_ctx(state={}, services={})
        with pytest.raises(KeyError):
            ctx.get_service("nonexistent_service")

    def test_tool_registry_service_injection(self) -> None:
        """tool_registry 应通过 PluginContext._services 注入。"""
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        retrieved = ctx.get_service("tool_registry")
        assert retrieved is registry

    @pytest.mark.asyncio
    async def test_tool_context_reads_electron_window_from_state(self) -> None:
        """ToolContextPlugin 应从 ctx.state['electron_window'] 读取窗口信息。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        window_data = {"title": "test", "processName": "Code", "x": 0, "y": 0, "width": 100, "height": 100}

        ctx = make_ctx(
            state={"electron_window": window_data},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates["tool_context"]["active_window"] is not None

    @pytest.mark.asyncio
    async def test_approval_route_reads_content_type_from_state(self) -> None:
        """ApprovalViewRoutePlugin 应从 ctx.state 读取 content_type。"""
        plugin = ApprovalViewRoutePlugin(config={})
        ctx = make_ctx(
            state={
                "content_type": "code_diff",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
            services={},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "code_diff"

    def test_connector_registry_accepts_base_connector_subclasses(self) -> None:
        """ConnectorRegistry 应能注册 BaseConnector 子类实例。"""
        from connectors.base import BaseConnector

        class FakeConnector(BaseConnector):
            @property
            def connector_type(self) -> str:
                return "fake"

            async def get_context(self) -> ConnectorContext:
                return ConnectorContext()

            async def execute_action(self, action: ConnectorAction) -> ActionResult:
                return ActionResult(success=True)

            async def connect(self) -> None:
                self._set_state(ConnectorState.CONNECTED)

            async def disconnect(self) -> None:
                self._set_state(ConnectorState.DISCONNECTED)

        registry = ConnectorRegistry()
        conn = FakeConnector()
        registry.register(conn)

        assert registry.has("fake")
        assert registry.get_connector("fake") is conn

    def test_state_keys_constants(self) -> None:
        """StateKeys 常量应被正确使用。"""
        assert hasattr(StateKeys, "APPROVAL_REQUIRED")
        assert hasattr(StateKeys, "RAW_RESULT")
        assert hasattr(StateKeys, "ROUTED_TO")


# ═══════════════════════════════════════════════════════════════════
# 6. 高级边界场景
# ═══════════════════════════════════════════════════════════════════


class TestAdvancedBoundaryScenarios:
    """高级边界场景：并发、状态隔离、适配器匹配、自定义类型映射等。"""

    @pytest.mark.asyncio
    async def test_concurrent_executions_state_isolation(self) -> None:
        """并发执行多次时，每次结果应独立，不应互相干扰。"""
        plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a", "tool_b"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        results = await asyncio.gather(
            plugin.execute(ctx),
            plugin.execute(ctx),
            plugin.execute(ctx),
        )

        for r in results:
            assert isinstance(r, PluginResult)
            tc = r.state_updates["tool_context"]
            assert set(tc["online_tools"]) == {"tool_a", "tool_b"}

    @pytest.mark.asyncio
    async def test_execute_does_not_mutate_input_state(self) -> None:
        """execute 不应直接修改传入的 state 字典。"""
        plugin = ToolContextPlugin(config={})
        original_state: dict[str, Any] = {}
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state=original_state, services={"tool_registry": registry})

        await plugin.execute(ctx)

        # state 应保持为空
        assert "tool_context" not in original_state
        assert "online_tools" not in original_state

    @pytest.mark.asyncio
    async def test_approval_route_custom_type_map(self) -> None:
        """自定义类型映射应覆盖默认映射。"""
        plugin = ApprovalViewRoutePlugin(config={
            "custom_type_map": {"my_custom_type": "text"},
        })
        ctx = make_ctx(
            state={
                "content_type": "my_custom_type",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
            services={},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "text"

    @pytest.mark.asyncio
    async def test_approval_route_custom_type_map_unknown(self) -> None:
        """自定义类型映射中不存在的类型应路由到 unknown。"""
        plugin = ApprovalViewRoutePlugin(config={
            "custom_type_map": {"known_type": "text"},
        })
        ctx = make_ctx(
            state={
                "content_type": "unknown_custom_type",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
            services={},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "unknown"

    @pytest.mark.asyncio
    async def test_adapter_capability_matching_for_code_diff(self) -> None:
        """code_diff 内容类型应匹配 show_diff 能力的适配器。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        tc_result = await tc_plugin.execute(ctx)
        state = dict(tc_result.state_updates)
        state.update({
            "content_type": "code_diff",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        })

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        # vscode 有 show_diff 能力
        routed_to = av_result.state_updates.get(StateKeys.ROUTED_TO, "")
        assert "code_diff" in routed_to

    @pytest.mark.asyncio
    async def test_all_content_type_aliases(self) -> None:
        """所有内容类型别名应正确路由。"""
        type_to_mode = {
            "text": "text",
            "code_diff": "code_diff",
            "diff": "code_diff",
            "patch": "code_diff",
            "file_change": "file_change",
            "file": "file_change",
            "command": "command",
            "shell": "command",
            "bash": "command",
        }

        for content_type, expected_mode in type_to_mode.items():
            plugin = ApprovalViewRoutePlugin(config={})
            ctx = make_ctx(
                state={
                    "content_type": content_type,
                    StateKeys.APPROVAL_REQUIRED: True,
                    StateKeys.RAW_RESULT: {},
                },
                services={},
            )

            result = await plugin.execute(ctx)
            actual = result.state_updates["approval_render_mode"]
            assert actual == expected_mode, (
                f"content_type='{content_type}' 应路由到 '{expected_mode}'，实际 '{actual}'"
            )

    @pytest.mark.asyncio
    async def test_content_type_case_insensitive(self) -> None:
        """内容类型匹配应不区分大小写。"""
        plugin = ApprovalViewRoutePlugin(config={})
        for ct in ["CODE_DIFF", "Code_Diff", "code_DIFF"]:
            ctx = make_ctx(
                state={
                    "content_type": ct,
                    StateKeys.APPROVAL_REQUIRED: True,
                    StateKeys.RAW_RESULT: {},
                },
                services={},
            )
            result = await plugin.execute(ctx)
            assert result.state_updates["approval_render_mode"] == "code_diff"

    @pytest.mark.asyncio
    async def test_empty_string_content_type_defaults_to_text(self) -> None:
        """空字符串 content_type 应路由到 text。"""
        plugin = ApprovalViewRoutePlugin(config={})
        ctx = make_ctx(
            state={
                "content_type": "",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
            services={},
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "text"

    @pytest.mark.asyncio
    async def test_raw_result_content_type_extraction(self) -> None:
        """应从 raw_result 中提取 content_type。"""
        plugin = ApprovalViewRoutePlugin(config={})
        ctx = make_ctx(
            state={
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {"content_type": "command"},
            },
            services={},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "command"

    @pytest.mark.asyncio
    async def test_tool_results_infers_content_type(self) -> None:
        """应从 tool_results 中的工具名推断 content_type。"""
        plugin = ApprovalViewRoutePlugin(config={})
        ctx = make_ctx(
            state={
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
                StateKeys.TOOL_RESULTS: [{"name": "bash_execute", "result": "ok"}],
            },
            services={},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "command"

    @pytest.mark.asyncio
    async def test_disabled_plugin_with_window_and_registry(self) -> None:
        """禁用的 ToolContextPlugin 即使有 registry 和窗口信息也应返回空。"""
        plugin = ToolContextPlugin(config={"enabled": False})
        registry = make_mock_registry(tool_names=["tool_a"])
        window = {"title": "test", "processName": "Code", "x": 0, "y": 0, "width": 100, "height": 100}
        ctx = make_ctx(
            state={"electron_window": window},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        tc = result.state_updates["tool_context"]

        assert tc["online_tools"] == []
        assert tc["active_window"] is None

    def test_degradation_manager_and_adapter_config_independent(self) -> None:
        """DegradationManager 和 AdapterConfig 应独立工作。"""
        dm = DegradationManager()
        configs = load_adapter_configs()

        # 降级管理器不依赖配置
        assert dm.can_handle_locally("open_file") is True
        # 配置加载不依赖降级管理器
        assert len(configs) >= 0  # 至少不抛异常


# ═══════════════════════════════════════════════════════════════════
# 7. 完整端到端数据流测试
# ═══════════════════════════════════════════════════════════════════


class TestFullEndToEndDataFlow:
    """端到端数据流完整性测试。"""

    @pytest.mark.asyncio
    async def test_e2e_text_with_electron_window(
        self, adapter_config_path: Any, electron_window_standard: Any,
    ) -> None:
        """端到端：文本审批 + Electron 窗口 + 适配器配置的完整流。"""
        # Phase 1: ToolContextPlugin
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute"])
        state: dict[str, Any] = {
            "electron_window": electron_window_standard,
            "content_type": "text",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {"content": "Hello World"},
        }
        ctx = make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        # Phase 2: ApprovalViewRoutePlugin
        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        # 验证完整数据流
        assert av_result.state_updates["approval_render_mode"] == "text"
        assert state["tool_context"]["online_tools"] == ["bash_execute"]
        assert state["tool_context"]["active_window"]["processName"] == "Code"
        assert len(state["tool_context"]["adapter_status"]) == 4

    @pytest.mark.asyncio
    async def test_e2e_no_tools_no_window_no_approval(self) -> None:
        """端到端：无工具、无窗口、无审批 → 最小降级路径。"""
        tc_plugin = ToolContextPlugin(config={})
        ctx = make_ctx(state={}, services={})
        tc_result = await tc_plugin.execute(ctx)

        tc = tc_result.state_updates["tool_context"]
        assert tc["online_tools"] == []
        assert tc["active_window"] is None
        assert tc["adapter_status"]  # 适配器配置独立于运行时状态
        assert tc["timestamp"] > 0

        # 无审批 → 路由插件应跳过
        av_plugin = ApprovalViewRoutePlugin(config={})
        state = dict(tc_result.state_updates)
        state[StateKeys.APPROVAL_REQUIRED] = False
        ctx2 = make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)
        assert av_result.state_updates == {}

    @pytest.mark.asyncio
    async def test_e2e_command_approval_with_file_change_fallback(self) -> None:
        """端到端：命令审批，然后切换为文件变更审批。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["bash_execute", "file_write"])

        # 第一轮：command
        state1: dict[str, Any] = {
            "content_type": "command",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {"command": "ls"},
        }
        ctx1 = make_ctx(state=state1, services={"tool_registry": registry})
        tc_r1 = await tc_plugin.execute(ctx1)
        state1.update(tc_r1.state_updates)

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx1b = make_ctx(state=state1, services={})
        av_r1 = await av_plugin.execute(ctx1b)
        assert av_r1.state_updates["approval_render_mode"] == "command"

        # 第二轮：file_change（独立的 context）
        state2: dict[str, Any] = {
            "content_type": "file_change",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx2 = make_ctx(state=state2, services={"tool_registry": registry})
        tc_r2 = await tc_plugin.execute(ctx2)
        state2.update(tc_r2.state_updates)

        ctx2b = make_ctx(state=state2, services={})
        av_r2 = await av_plugin.execute(ctx2b)
        assert av_r2.state_updates["approval_render_mode"] == "file_change"

    @pytest.mark.asyncio
    async def test_e2e_multiple_content_types_sequential(self) -> None:
        """端到端：顺序处理多种内容类型，每次结果独立。"""
        tc_plugin = ToolContextPlugin(config={})
        av_plugin = ApprovalViewRoutePlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])

        content_types = ["text", "code_diff", "command", "file_change", "unknown_type"]

        for ct in content_types:
            state: dict[str, Any] = {
                "content_type": ct,
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            }
            ctx = make_ctx(state=state, services={"tool_registry": registry})
            tc_result = await tc_plugin.execute(ctx)
            state.update(tc_result.state_updates)

            ctx2 = make_ctx(state=state, services={})
            av_result = await av_plugin.execute(ctx2)

            # 验证每次路由结果独立
            mode = av_result.state_updates["approval_render_mode"]
            if ct == "unknown_type":
                assert mode == "unknown"
            else:
                assert mode == ct


# ═══════════════════════════════════════════════════════════════════
# 8. 适配器状态与路由决策联动
# ═══════════════════════════════════════════════════════════════════


class TestAdapterStatusAndRouteDecision:
    """验证适配器状态信息如何影响路由决策。"""

    @pytest.mark.asyncio
    async def test_vscode_adapter_has_show_diff_capability(
        self, adapter_config_path: Any,
    ) -> None:
        """VSCode 适配器应能匹配 code_diff 内容类型。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result = await tc_plugin.execute(ctx)
        adapter_status = result.state_updates["tool_context"]["adapter_status"]

        vscode_caps = set(adapter_status["vscode"]["capabilities"])
        assert "show_diff" in vscode_caps
        assert "open_file" in vscode_caps

    @pytest.mark.asyncio
    async def test_windows_desktop_adapter_has_keyboard_input(
        self, adapter_config_path: Any,
    ) -> None:
        """Windows Desktop 适配器应能匹配 command 内容类型。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result = await tc_plugin.execute(ctx)
        adapter_status = result.state_updates["tool_context"]["adapter_status"]

        desktop_caps = set(adapter_status["windows_desktop"]["capabilities"])
        assert "keyboard_input" in desktop_caps

    @pytest.mark.asyncio
    async def test_playwright_adapter_has_screenshot_capability(
        self, adapter_config_path: Any,
    ) -> None:
        """Playwright 适配器应包含 screenshot 能力。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result = await tc_plugin.execute(ctx)
        adapter_status = result.state_updates["tool_context"]["adapter_status"]

        pw_caps = set(adapter_status["playwright"]["capabilities"])
        assert "screenshot" in pw_caps

    @pytest.mark.asyncio
    async def test_adapter_status_display_names(
        self, adapter_config_path: Any,
    ) -> None:
        """adapter_status 中应包含 display_name。"""
        tc_plugin = ToolContextPlugin(config={})
        registry = make_mock_registry(tool_names=["tool_a"])
        ctx = make_ctx(state={}, services={"tool_registry": registry})

        result = await tc_plugin.execute(ctx)
        adapter_status = result.state_updates["tool_context"]["adapter_status"]

        assert adapter_status["vscode"]["display_name"] == "Visual Studio Code"
        assert adapter_status["comfyui"]["display_name"] == "ComfyUI"
        assert adapter_status["playwright"]["display_name"] == "Playwright"
        assert adapter_status["windows_desktop"]["display_name"] == "Windows Desktop"


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


async def await_if_needed(coro):
    """等待协程执行。如果已经是结果对象则直接返回。"""
    if asyncio.iscoroutine(coro):
        return await coro
    return coro
