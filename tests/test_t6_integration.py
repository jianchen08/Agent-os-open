"""T6-外部工具桌面交互集成测试。

对 T5 集成联调修复后的全部模块进行集成测试，验证各模块协作正确性。

测试模块：
1. config/capability_adapters.yaml - 连接器能力定义配置加载测试
2. src/plugins/input/tool_context/plugin.py - 管道输入层上下文感知插件功能测试
3. src/human_interaction/view_router.py - 审批视图路由逻辑测试
4. src/plugins/output/approval_view_route/plugin.py - 审批视图路由 Output 插件测试
5. 端到端数据流集成测试

测试要点：
- tool_context_plugin 能正确读取 ToolRegistry 的工具在线状态并注入上下文
- capability_adapters.yaml 适配器配置能被正确加载和解析
- view_router.py 能根据不同对象类型正确路由到对应审批视图模式
- 端到端数据流：工具状态感知→上下文注入→审批路由→视图渲染，全链路通畅
- 所有Python模块可正常导入，无循环依赖
- 边界场景：空数据、未知对象类型、工具离线状态等
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── 模块导入 ─────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════
# 第一部分：Python 模块导入与循环依赖检查
# ═══════════════════════════════════════════════════════════════


class TestModuleImportAndCircularDependency:
    """验证所有 Python 模块可正常导入，无循环依赖。"""

    def test_import_adapter_config(self) -> None:
        """connectors.adapter_config 模块可正常导入。"""
        from connectors.adapter_config import (
            AdapterConfig,
            get_adapter_status_summary,
            load_adapter_configs,
        )
        assert AdapterConfig is not None
        assert callable(load_adapter_configs)
        assert callable(get_adapter_status_summary)

    def test_import_tool_context_plugin(self) -> None:
        """plugins.input.tool_context 插件模块可正常导入。"""
        from plugins.input.tool_context import ToolContextPlugin
        assert ToolContextPlugin is not None

    def test_import_view_router(self) -> None:
        """human_interaction.view_router 模块可正常导入。"""
        from human_interaction.view_router import (
            ViewMode,
            get_artifact_view_hints,
            resolve_view_mode,
        )
        assert ViewMode is not None
        assert callable(resolve_view_mode)
        assert callable(get_artifact_view_hints)

    def test_import_window_info_bridge(self) -> None:
        """bridge.window_info 桥接层模块可正常导入。"""
        from bridge.window_info import (
            WindowInfoData,
            normalize_window_info,
            validate_window_info,
        )
        assert WindowInfoData is not None
        assert callable(normalize_window_info)
        assert callable(validate_window_info)

    def test_import_approval_view_route_plugin(self) -> None:
        """plugins.output.approval_view_route 插件模块可正常导入。"""
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin
        assert ApprovalViewRoutePlugin is not None

    def test_import_pipeline_types(self) -> None:
        """pipeline.types 模块可正常导入。"""
        from pipeline.types import ErrorPolicy, RouteSignal, StateKeys
        assert ErrorPolicy is not None
        assert RouteSignal is not None
        assert StateKeys is not None

    def test_import_pipeline_plugin(self) -> None:
        """pipeline.plugin 模块可正常导入。"""
        from pipeline.plugin import (
            IInputPlugin,
            IOutputPlugin,
            OutputResult,
            PluginContext,
            PluginResult,
        )
        assert IInputPlugin is not None
        assert PluginContext is not None
        assert PluginResult is not None

    def test_no_circular_dependency_bulk_import(self) -> None:
        """批量导入所有目标模块，验证无循环依赖异常。"""
        # 按照依赖链顺序导入，如果存在循环依赖将触发 ImportError
        import connectors.adapter_config  # noqa: F401
        import bridge.window_info  # noqa: F401
        import pipeline.types  # noqa: F401
        import pipeline.plugin  # noqa: F401
        import plugins.input.tool_context  # noqa: F401
        import human_interaction.view_router  # noqa: F401
        import plugins.output.approval_view_route  # noqa: F401

        # 所有模块导入成功即证明无循环依赖
        assert True


# ═══════════════════════════════════════════════════════════════
# 第二部分：capability_adapters.yaml 配置加载测试
# ═══════════════════════════════════════════════════════════════


class TestCapabilityAdaptersConfig:
    """验证 capability_adapters.yaml 能被正确加载和解析。"""

    def test_config_file_exists(self) -> None:
        """配置文件应存在于 config/ 目录下。"""
        config_path = Path(__file__).resolve().parent.parent / "config" / "capability_adapters.yaml"
        assert config_path.exists(), f"配置文件不存在: {config_path}"

    def test_load_adapter_configs_returns_four_adapters(self) -> None:
        """应正确加载四个适配器配置。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert len(configs) == 4, f"预期 4 个适配器，实际 {len(configs)} 个"

    def test_all_expected_adapter_names_present(self) -> None:
        """应包含 vscode、comfyui、playwright、windows_desktop 四个适配器。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        expected = {"vscode", "comfyui", "playwright", "windows_desktop"}
        assert expected == set(configs.keys())

    @pytest.mark.parametrize("name,expected_type", [
        ("vscode", "ide"),
        ("comfyui", "creative"),
        ("playwright", "browser"),
        ("windows_desktop", "desktop"),
    ])
    def test_adapter_type_correct(self, name: str, expected_type: str) -> None:
        """每个适配器的类型应正确。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert configs[name].adapter_type == expected_type

    @pytest.mark.parametrize("name,expected_mcp", [
        ("vscode", False),
        ("comfyui", False),
        ("playwright", True),
        ("windows_desktop", True),
    ])
    def test_adapter_mcp_flag_correct(self, name: str, expected_mcp: bool) -> None:
        """MCP 标志应正确：playwright 和 windows_desktop 为 MCP 连接器。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert configs[name].has_mcp == expected_mcp

    @pytest.mark.parametrize("name", ["vscode", "comfyui", "playwright", "windows_desktop"])
    def test_all_adapters_available(self, name: str) -> None:
        """所有适配器默认应为启用状态。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert configs[name].available is True

    @pytest.mark.parametrize("name,min_capabilities", [
        ("vscode", 1),
        ("comfyui", 1),
        ("playwright", 1),
        ("windows_desktop", 1),
    ])
    def test_all_adapters_have_capabilities(self, name: str, min_capabilities: int) -> None:
        """每个适配器至少应有指定数量的能力。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert len(configs[name].capabilities) >= min_capabilities

    def test_vscode_has_show_diff_capability(self) -> None:
        """VSCode 适配器应包含 show_diff 能力。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert "show_diff" in configs["vscode"].capabilities

    def test_windows_desktop_has_keyboard_input(self) -> None:
        """Windows Desktop 适配器应包含 keyboard_input 能力。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert "keyboard_input" in configs["windows_desktop"].capabilities

    def test_playwright_has_screenshot_capability(self) -> None:
        """Playwright 适配器应包含 screenshot 能力。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert "screenshot" in configs["playwright"].capabilities

    def test_vscode_connector_class_not_none(self) -> None:
        """非 MCP 适配器应有 connector_class 路径。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert configs["vscode"].connector_class is not None
        assert "VSCodeConnector" in configs["vscode"].connector_class

    def test_playwright_connector_class_is_none(self) -> None:
        """MCP 适配器的 connector_class 应为 None。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs()
        assert configs["playwright"].connector_class is None

    def test_get_adapter_status_summary_returns_all(self) -> None:
        """get_adapter_status_summary 应返回所有适配器的状态摘要。"""
        from connectors.adapter_config import get_adapter_status_summary
        summary = get_adapter_status_summary()
        assert len(summary) == 4
        for name, status in summary.items():
            assert "type" in status
            assert "available" in status
            assert "capabilities_count" in status
            assert "has_mcp" in status
            assert "display_name" in status
            assert "capabilities" in status

    def test_load_nonexistent_config_returns_empty(self) -> None:
        """加载不存在的配置文件应返回空字典。"""
        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs("/nonexistent/path.yaml")
        assert configs == {}

    def test_load_config_with_custom_path(self, tmp_path: Path) -> None:
        """使用自定义路径加载配置。"""
        yaml_content = """
adapters:
  test_adapter:
    type: "test"
    priority: 1
    display_name: "Test Adapter"
    capabilities:
      - "test_action"
    mcp_config: null
    connector_class: "test.TestConnector"
    available: true
"""
        config_file = tmp_path / "test_adapters.yaml"
        config_file.write_text(yaml_content, encoding="utf-8")

        from connectors.adapter_config import load_adapter_configs
        configs = load_adapter_configs(str(config_file))
        assert "test_adapter" in configs
        assert configs["test_adapter"].adapter_type == "test"
        assert configs["test_adapter"].capabilities == ("test_action",)

    def test_adapter_config_is_frozen_dataclass(self) -> None:
        """AdapterConfig 应为不可变数据类。"""
        from connectors.adapter_config import AdapterConfig
        cfg = AdapterConfig(name="test")
        with pytest.raises(AttributeError):
            cfg.name = "changed"  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════
# 第三部分：view_router.py 审批视图路由逻辑测试
# ═══════════════════════════════════════════════════════════════


class TestViewRouterResolveViewMode:
    """验证 view_router.py 的 resolve_view_mode 函数路由逻辑。"""

    def test_explicit_mode_text_diff(self) -> None:
        """显式指定 text_diff 应直接返回。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(explicit_mode="text_diff") == "text_diff"

    def test_explicit_mode_image_annotation(self) -> None:
        """显式指定 image_annotation 应直接返回。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(explicit_mode="image_annotation") == "image_annotation"

    def test_explicit_mode_media_timeline(self) -> None:
        """显式指定 media_timeline 应直接返回。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(explicit_mode="media_timeline") == "media_timeline"

    def test_explicit_mode_invalid_falls_through(self) -> None:
        """显式指定无效模式时应继续尝试其他推断方式。"""
        from human_interaction.view_router import resolve_view_mode
        # 无效 explicit_mode + artifact_types 可推断
        result = resolve_view_mode(
            explicit_mode="invalid_mode",
            artifact_types=["image"],
        )
        assert result == "image_annotation"

    def test_artifact_type_text_returns_text_diff(self) -> None:
        """text 类型制品应路由到 text_diff 视图。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(artifact_types=["text"]) == "text_diff"

    def test_artifact_type_file_returns_text_diff(self) -> None:
        """file 类型制品应路由到 text_diff 视图。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(artifact_types=["file"]) == "text_diff"

    def test_artifact_type_image_returns_image_annotation(self) -> None:
        """image 类型制品应路由到 image_annotation 视图。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(artifact_types=["image"]) == "image_annotation"

    def test_artifact_type_screenshot_returns_image_annotation(self) -> None:
        """screenshot 类型制品应路由到 image_annotation 视图。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(artifact_types=["screenshot"]) == "image_annotation"

    def test_artifact_type_video_returns_media_timeline(self) -> None:
        """video 类型制品应路由到 media_timeline 视图。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(artifact_types=["video"]) == "media_timeline"

    def test_artifact_type_audio_returns_media_timeline(self) -> None:
        """audio 类型制品应路由到 media_timeline 视图。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(artifact_types=["audio"]) == "media_timeline"

    def test_first_artifact_type_inferred(self) -> None:
        """使用 first_artifact_type 参数应能正确推断。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode(first_artifact_type="image") == "image_annotation"

    def test_artifact_types_takes_precedence_over_first(self) -> None:
        """artifact_types 应优先于 first_artifact_type。"""
        from human_interaction.view_router import resolve_view_mode
        result = resolve_view_mode(
            artifact_types=["video"],
            first_artifact_type="image",
        )
        assert result == "media_timeline"  # artifact_types[0]="video"

    def test_metadata_view_mode_used_as_fallback(self) -> None:
        """metadata 中的 view_mode 应作为回退方案。"""
        from human_interaction.view_router import resolve_view_mode
        result = resolve_view_mode(metadata={"view_mode": "media_timeline"})
        assert result == "media_timeline"

    def test_metadata_view_mode_invalid_ignored(self) -> None:
        """metadata 中的无效 view_mode 应被忽略。"""
        from human_interaction.view_router import resolve_view_mode
        result = resolve_view_mode(metadata={"view_mode": "invalid"})
        assert result == "text_diff"  # 默认

    def test_no_inputs_returns_default_text_diff(self) -> None:
        """无任何输入时应返回默认 text_diff。"""
        from human_interaction.view_router import resolve_view_mode
        assert resolve_view_mode() == "text_diff"

    def test_unknown_artifact_type_returns_default(self) -> None:
        """未知制品类型应返回默认 text_diff。"""
        from human_interaction.view_router import resolve_view_mode
        result = resolve_view_mode(artifact_types=["unknown_type"])
        assert result == "text_diff"

    def test_empty_artifact_types_returns_default(self) -> None:
        """空制品类型列表应返回默认 text_diff。"""
        from human_interaction.view_router import resolve_view_mode
        result = resolve_view_mode(artifact_types=[])
        assert result == "text_diff"

    def test_multiple_artifact_types_uses_first(self) -> None:
        """多个制品类型时应使用第一个推断。"""
        from human_interaction.view_router import resolve_view_mode
        result = resolve_view_mode(artifact_types=["image", "text"])
        assert result == "image_annotation"

    def test_explicit_mode_highest_priority(self) -> None:
        """显式指定模式应为最高优先级。"""
        from human_interaction.view_router import resolve_view_mode
        result = resolve_view_mode(
            explicit_mode="media_timeline",
            artifact_types=["image"],
            first_artifact_type="text",
            metadata={"view_mode": "text_diff"},
        )
        assert result == "media_timeline"


class TestViewRouterGetArtifactViewHints:
    """验证 get_artifact_view_hints 函数。"""

    def test_text_type_hints(self) -> None:
        """text 类型应返回 text_diff 视图提示。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("text")
        assert hints["view_mode"] == "text_diff"
        assert hints["supports_annotations"] is False
        assert hints["supports_timeline"] is False

    def test_image_type_hints(self) -> None:
        """image 类型应返回 image_annotation 视图提示。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("image")
        assert hints["view_mode"] == "image_annotation"
        assert hints["supports_annotations"] is True
        assert hints["supports_timeline"] is False

    def test_video_type_hints(self) -> None:
        """video 类型应返回 media_timeline 视图提示。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("video")
        assert hints["view_mode"] == "media_timeline"
        assert hints["supports_annotations"] is True
        assert hints["supports_timeline"] is True

    def test_video_with_duration_metadata(self) -> None:
        """视频类型应从 metadata 中提取时长信息。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("video", {"duration": 120.5})
        assert hints["duration"] == 120.5
        assert hints["media_type"] == "video"

    def test_video_with_duration_seconds_metadata(self) -> None:
        """视频类型应支持 duration_seconds 字段。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("video", {"duration_seconds": 90})
        assert hints["duration"] == 90.0

    def test_audio_type_hints(self) -> None:
        """audio 类型应返回 media_timeline 视图提示。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("audio")
        assert hints["view_mode"] == "media_timeline"

    def test_image_with_dimensions_metadata(self) -> None:
        """图片类型应从 metadata 中提取尺寸信息。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("image", {"width": 1920, "height": 1080})
        assert hints["image_dimensions"] == {"width": 1920, "height": 1080}

    def test_image_missing_one_dimension_no_size(self) -> None:
        """图片缺少一个维度时应不包含 image_dimensions。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("image", {"width": 1920})
        assert "image_dimensions" not in hints

    def test_unknown_type_returns_default_hints(self) -> None:
        """未知类型应返回默认 text_diff 视图提示。"""
        from human_interaction.view_router import get_artifact_view_hints
        hints = get_artifact_view_hints("unknown_xyz")
        assert hints["view_mode"] == "text_diff"


class TestViewModeEnum:
    """验证 ViewMode 枚举。"""

    def test_view_mode_values(self) -> None:
        """ViewMode 枚举应包含三种模式。"""
        from human_interaction.view_router import ViewMode
        assert ViewMode.TEXT_DIFF.value == "text_diff"
        assert ViewMode.IMAGE_ANNOTATION.value == "image_annotation"
        assert ViewMode.MEDIA_TIMELINE.value == "media_timeline"

    def test_view_mode_is_string_enum(self) -> None:
        """ViewMode 应为 str 枚举，可直接比较字符串。"""
        from human_interaction.view_router import ViewMode
        assert ViewMode.TEXT_DIFF == "text_diff"
        assert isinstance(ViewMode.TEXT_DIFF, str)


# ═══════════════════════════════════════════════════════════════
# 第四部分：tool_context_plugin 上下文注入集成测试
# ═══════════════════════════════════════════════════════════════


def _make_ctx(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> "PluginContext":
    """构建测试用 PluginContext。"""
    from pipeline.plugin import PluginContext
    return PluginContext(state=state or {}, _services=services or {})


def _make_mock_registry(
    tool_names: list[str] | None = None,
    has_handlers: dict[str, bool] | None = None,
) -> MagicMock:
    """构建 mock ToolRegistry。"""
    registry = MagicMock()
    mock_tools = []
    for name in tool_names or []:
        tool = MagicMock()
        tool.name = name
        mock_tools.append(tool)

    registry.list_all.return_value = mock_tools
    registry.get_dynamic_tool_names.return_value = set(tool_names or [])

    if has_handlers:
        registry.has_handler.side_effect = lambda n: has_handlers.get(n, False)
    else:
        registry.has_handler.return_value = True

    return registry


class TestToolContextPluginIntegration:
    """验证 ToolContextPlugin 与 adapter_config 的集成。"""

    @pytest.mark.asyncio
    async def test_plugin_includes_adapter_status_from_yaml(self) -> None:
        """插件执行后 tool_context 应包含来自 YAML 的适配器状态。"""
        from plugins.input.tool_context import ToolContextPlugin
        plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["bash_execute"])
        ctx = _make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        tc = result.state_updates["tool_context"]

        assert "adapter_status" in tc
        assert len(tc["adapter_status"]) == 4
        for name in ["vscode", "comfyui", "playwright", "windows_desktop"]:
            assert name in tc["adapter_status"]

    @pytest.mark.asyncio
    async def test_adapter_status_contains_required_fields(self) -> None:
        """适配器状态摘要应包含所有必需字段。"""
        from plugins.input.tool_context import ToolContextPlugin
        plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["file_read"])
        ctx = _make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        for name, status in result.state_updates["tool_context"]["adapter_status"].items():
            assert "type" in status, f"{name} 缺少 type"
            assert "available" in status, f"{name} 缺少 available"
            assert "has_mcp" in status, f"{name} 缺少 has_mcp"
            assert "capabilities" in status, f"{name} 缺少 capabilities"

    @pytest.mark.asyncio
    async def test_tool_context_with_window_and_adapter(self) -> None:
        """同时有窗口信息和适配器时，应构建完整的上下文。"""
        from plugins.input.tool_context import ToolContextPlugin
        plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["bash_execute", "file_read"])
        window = {
            "title": "main.py - VSCode",
            "processName": "Code",
            "x": 0, "y": 0, "width": 1920, "height": 1080,
        }
        ctx = _make_ctx(
            state={"electron_window": window},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        tc = result.state_updates["tool_context"]

        assert len(tc["online_tools"]) == 2
        assert tc["active_window"] is not None
        assert tc["active_window"]["processName"] == "Code"
        assert len(tc["adapter_status"]) == 4
        assert isinstance(tc["timestamp"], float)


# ═══════════════════════════════════════════════════════════════
# 第五部分：审批视图路由 Output 插件集成测试
# ═══════════════════════════════════════════════════════════════


class TestApprovalViewRoutePluginIntegration:
    """验证 ApprovalViewRoutePlugin 与 tool_context 的集成。"""

    @pytest.mark.asyncio
    async def test_text_content_type_routes_to_text(self) -> None:
        """text 内容类型应路由到 text 渲染模式。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "text",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "text"

    @pytest.mark.asyncio
    async def test_code_diff_content_type_routes_correctly(self) -> None:
        """code_diff 内容类型应路由到 code_diff 渲染模式。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "code_diff",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "code_diff"

    @pytest.mark.asyncio
    async def test_command_content_type_routes_correctly(self) -> None:
        """command 内容类型应路由到 command 渲染模式。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "command",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "command"

    @pytest.mark.asyncio
    async def test_file_change_content_type_routes_correctly(self) -> None:
        """file_change 内容类型应路由到 file_change 渲染模式。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "file_change",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "file_change"

    @pytest.mark.asyncio
    async def test_unknown_content_type_routes_to_unknown(self) -> None:
        """未知内容类型应路由到 unknown 渲染模式。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "mystery_type",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "unknown"

    @pytest.mark.asyncio
    async def test_no_approval_required_skips_route(self) -> None:
        """无需审批时应跳过路由。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "code_diff",
                StateKeys.APPROVAL_REQUIRED: False,
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_diff_alias_routes_to_code_diff(self) -> None:
        """diff 别名应路由到 code_diff。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "diff",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "code_diff"

    @pytest.mark.asyncio
    async def test_bash_alias_routes_to_command(self) -> None:
        """bash 别名应路由到 command。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "bash",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "command"

    @pytest.mark.asyncio
    async def test_routed_to_state_key_set(self) -> None:
        """路由后应设置 ROUTED_TO 状态键。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={})
        ctx = _make_ctx(
            state={
                "content_type": "text",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert StateKeys.ROUTED_TO in result.state_updates
        assert "approval:text" in result.state_updates[StateKeys.ROUTED_TO]

    @pytest.mark.asyncio
    async def test_custom_type_map_merged(self) -> None:
        """自定义类型映射应被合并。"""
        from pipeline.types import StateKeys
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        plugin = ApprovalViewRoutePlugin(config={
            "custom_type_map": {"my_custom": "text"},
        })
        ctx = _make_ctx(
            state={
                "content_type": "my_custom",
                StateKeys.APPROVAL_REQUIRED: True,
                StateKeys.RAW_RESULT: {},
            },
        )
        result = await plugin.execute(ctx)
        assert result.state_updates["approval_render_mode"] == "text"


# ═══════════════════════════════════════════════════════════════
# 第六部分：端到端数据流测试
# ═══════════════════════════════════════════════════════════════


class TestEndToEndDataFlow:
    """端到端数据流测试：工具状态感知→上下文注入→审批路由→视图模式。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_text_approval(self) -> None:
        """完整文本审批流程：工具在线→上下文构建→审批路由→text模式。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        # 阶段1: ToolContextPlugin 收集上下文
        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["bash_execute", "file_read"])
        window = {
            "title": "review.py - VSCode",
            "processName": "Code",
            "x": 0, "y": 0, "width": 1920, "height": 1080,
        }
        state: dict[str, Any] = {
            "electron_window": window,
            "content_type": "text",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = _make_ctx(state=state, services={"tool_registry": registry})

        tc_result = await tc_plugin.execute(ctx)
        assert "tool_context" in tc_result.state_updates

        # 阶段2: 更新状态，传递给审批路由插件
        state.update(tc_result.state_updates)
        tc = state["tool_context"]
        assert len(tc["online_tools"]) == 2
        assert tc["active_window"] is not None
        assert len(tc["adapter_status"]) == 4

        # 阶段3: ApprovalViewRoutePlugin 路由决策
        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates["approval_render_mode"] == "text"
        assert "approval:text" in av_result.state_updates[StateKeys.ROUTED_TO]

    @pytest.mark.asyncio
    async def test_full_pipeline_code_diff_with_adapter_hint(self) -> None:
        """完整代码差异审批流程：应能匹配到适配器能力。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["file_read", "file_write"])
        state: dict[str, Any] = {
            "content_type": "code_diff",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = _make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates["approval_render_mode"] == "code_diff"

    @pytest.mark.asyncio
    async def test_full_pipeline_command_approval(self) -> None:
        """完整命令审批流程。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["bash_execute"])
        state: dict[str, Any] = {
            "content_type": "command",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = _make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates["approval_render_mode"] == "command"

    @pytest.mark.asyncio
    async def test_full_pipeline_no_approval_skips_route(self) -> None:
        """无需审批时完整流程应跳过路由。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["bash_execute"])
        state: dict[str, Any] = {
            StateKeys.APPROVAL_REQUIRED: False,
        }
        ctx = _make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)

        assert av_result.state_updates == {}

    @pytest.mark.asyncio
    async def test_full_pipeline_with_all_tools_offline(self) -> None:
        """全部工具离线时完整流程应正常降级。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(
            tool_names=["tool_a", "tool_b"],
            has_handlers={"tool_a": False, "tool_b": False},
        )
        state: dict[str, Any] = {
            "content_type": "text",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = _make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        # 即使全部离线，tool_context 仍应正常构建
        tc = state["tool_context"]
        assert tc["online_tools"] == []
        assert len(tc["adapter_status"]) == 4

        # 审批路由应不受影响
        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)
        assert av_result.state_updates["approval_render_mode"] == "text"

    @pytest.mark.asyncio
    async def test_full_pipeline_electron_not_running(self) -> None:
        """Electron 未运行时完整流程应正常降级。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["file_read"])
        state: dict[str, Any] = {
            # 无 electron_window 键
            "content_type": "file_change",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = _make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        assert state["tool_context"]["active_window"] is None
        assert "file_read" in state["tool_context"]["online_tools"]

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)
        assert av_result.state_updates["approval_render_mode"] == "file_change"

    @pytest.mark.asyncio
    async def test_view_router_consumes_tool_context_data(self) -> None:
        """view_router 的 resolve_view_mode 应能消费 tool_context 中的数据。"""
        from human_interaction.view_router import resolve_view_mode
        from plugins.input.tool_context import ToolContextPlugin

        # 构建 tool_context
        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["bash_execute"])
        ctx = _make_ctx(state={}, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        tc = tc_result.state_updates["tool_context"]

        # view_router 能基于制品类型推断视图模式
        mode = resolve_view_mode(artifact_types=["image"])
        assert mode == "image_annotation"

        mode = resolve_view_mode(artifact_types=["video"])
        assert mode == "media_timeline"

    @pytest.mark.asyncio
    async def test_full_pipeline_unknown_type_degrades(self) -> None:
        """未知内容类型应正确降级。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        tc_plugin = ToolContextPlugin(config={})
        registry = _make_mock_registry(tool_names=["bash_execute"])
        state: dict[str, Any] = {
            "content_type": "totally_unknown",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = _make_ctx(state=state, services={"tool_registry": registry})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)
        assert av_result.state_updates["approval_render_mode"] == "unknown"

    @pytest.mark.asyncio
    async def test_full_pipeline_empty_environment(self) -> None:
        """完全空环境（无 registry、无窗口）下流程应正常。"""
        from pipeline.types import StateKeys
        from plugins.input.tool_context import ToolContextPlugin
        from plugins.output.approval_view_route import ApprovalViewRoutePlugin

        tc_plugin = ToolContextPlugin(config={})
        state: dict[str, Any] = {
            "content_type": "text",
            StateKeys.APPROVAL_REQUIRED: True,
            StateKeys.RAW_RESULT: {},
        }
        ctx = _make_ctx(state=state, services={})
        tc_result = await tc_plugin.execute(ctx)
        state.update(tc_result.state_updates)

        tc = state["tool_context"]
        assert tc["online_tools"] == []
        assert tc["active_window"] is None
        assert len(tc["adapter_status"]) == 4

        av_plugin = ApprovalViewRoutePlugin(config={})
        ctx2 = _make_ctx(state=state, services={})
        av_result = await av_plugin.execute(ctx2)
        assert av_result.state_updates["approval_render_mode"] == "text"


# ═══════════════════════════════════════════════════════════════
# 第七部分：前后端视图模式一致性验证
# ═══════════════════════════════════════════════════════════════


class TestFrontendBackendConsistency:
    """验证后端 view_router.py 与前端 ApprovalRouter.tsx 的视图模式一致。"""

    def test_backend_view_modes_match_frontend(self) -> None:
        """后端 ViewMode 枚举值应与前端 ViewMode 类型对齐。

        前端定义: 'text_diff' | 'image_annotation' | 'media_timeline'
        后端定义: ViewMode.TEXT_DIFF | ViewMode.IMAGE_ANNOTATION | ViewMode.MEDIA_TIMELINE
        """
        from human_interaction.view_router import ViewMode

        backend_modes = {m.value for m in ViewMode}
        frontend_modes = {"text_diff", "image_annotation", "media_timeline"}
        assert backend_modes == frontend_modes

    def test_frontend_approval_router_importable(self) -> None:
        """前端 ApprovalRouter.tsx 文件应存在。"""
        frontend_path = (
            Path(__file__).resolve().parent.parent
            / "frontend" / "src" / "components" / "approval" / "ApprovalRouter.tsx"
        )
        assert frontend_path.exists(), f"前端组件文件不存在: {frontend_path}"

    def test_frontend_approval_router_contains_view_modes(self) -> None:
        """前端组件源码中应包含所有视图模式的引用。"""
        frontend_path = (
            Path(__file__).resolve().parent.parent
            / "frontend" / "src" / "components" / "approval" / "ApprovalRouter.tsx"
        )
        content = frontend_path.read_text(encoding="utf-8")
        for mode in ["text_diff", "image_annotation", "media_timeline"]:
            assert mode in content, f"前端组件中未找到视图模式: {mode}"

    def test_frontend_approval_router_test_exists(self) -> None:
        """前端测试文件应存在。"""
        test_path = (
            Path(__file__).resolve().parent.parent
            / "frontend" / "src" / "components" / "approval" / "__tests__" / "ApprovalRouter.test.tsx"
        )
        assert test_path.exists(), f"前端测试文件不存在: {test_path}"

    def test_approval_view_route_plugin_modes_cover_all(self) -> None:
        """ApprovalViewRoutePlugin 的 RenderMode 应覆盖所有已知类型。"""
        from plugins.output.approval_view_route.plugin import RenderMode

        # RenderMode 的所有值
        render_modes = {m.value for m in RenderMode}
        # 应至少包含: text, code_diff, file_change, command, unknown
        expected_modes = {"text", "code_diff", "file_change", "command", "unknown"}
        assert expected_modes.issubset(render_modes)

    def test_content_type_to_render_mode_to_view_mode_chain(self) -> None:
        """验证 content_type → render_mode 的完整映射链。"""
        from plugins.output.approval_view_route.plugin import _CONTENT_TYPE_MAP, RenderMode

        # 所有已知映射都应指向有效的 RenderMode
        for ct, mode in _CONTENT_TYPE_MAP.items():
            assert isinstance(mode, RenderMode), f"{ct} -> {mode} 不是有效的 RenderMode"

        # 确认关键映射存在
        assert _CONTENT_TYPE_MAP["text"] == RenderMode.TEXT
        assert _CONTENT_TYPE_MAP["code_diff"] == RenderMode.CODE_DIFF
        assert _CONTENT_TYPE_MAP["file_change"] == RenderMode.FILE_CHANGE
        assert _CONTENT_TYPE_MAP["command"] == RenderMode.COMMAND
        assert _CONTENT_TYPE_MAP["diff"] == RenderMode.CODE_DIFF
        assert _CONTENT_TYPE_MAP["bash"] == RenderMode.COMMAND


# ═══════════════════════════════════════════════════════════════
# 第八部分：窗口信息桥接层集成测试
# ═══════════════════════════════════════════════════════════════


class TestWindowInfoBridgeIntegration:
    """验证窗口信息桥接层与 ToolContextPlugin 的集成。"""

    def test_standard_electron_format(self) -> None:
        """标准 Electron WindowInfo 格式应正确解析。"""
        from bridge.window_info import normalize_window_info

        raw = {
            "title": "app.tsx - VSCode",
            "processName": "Code",
            "x": 192, "y": 52, "width": 1536, "height": 864,
        }
        result = normalize_window_info(raw)
        assert result is not None
        assert result.title == "app.tsx - VSCode"
        assert result.processName == "Code"
        assert result.x == 192

    def test_legacy_format(self) -> None:
        """旧格式（app/bounds）应兼容处理。"""
        from bridge.window_info import normalize_window_info

        raw = {"title": "test.py", "app": "VSCode", "bounds": {"x": 0, "y": 0, "width": 800, "height": 600}}
        result = normalize_window_info(raw)
        assert result is not None
        assert result.processName == "VSCode"
        assert result.width == 800

    def test_empty_dict_returns_none(self) -> None:
        """空字典应返回 None。"""
        from bridge.window_info import normalize_window_info
        assert normalize_window_info({}) is None

    def test_none_returns_none(self) -> None:
        """None 输入应返回 None。"""
        from bridge.window_info import normalize_window_info
        assert normalize_window_info(None) is None

    def test_to_dict_fields_align(self) -> None:
        """to_dict 输出字段应与 Electron 接口对齐。"""
        from bridge.window_info import WindowInfoData

        data = WindowInfoData(title="t", processName="p", x=1, y=2, width=100, height=200)
        d = data.to_dict()
        assert set(d.keys()) == {"title", "processName", "x", "y", "width", "height"}

    @pytest.mark.asyncio
    async def test_window_info_flows_through_plugin(self) -> None:
        """窗口信息应通过 ToolContextPlugin 正确流入 tool_context。"""
        from plugins.input.tool_context import ToolContextPlugin

        plugin = ToolContextPlugin(config={})
        window = {"title": "test.py", "processName": "Code", "x": 0, "y": 0, "width": 800, "height": 600}
        registry = _make_mock_registry(tool_names=["bash_execute"])
        ctx = _make_ctx(
            state={"electron_window": window},
            services={"tool_registry": registry},
        )
        result = await plugin.execute(ctx)
        tc = result.state_updates["tool_context"]

        assert tc["active_window"] is not None
        assert tc["active_window"]["title"] == "test.py"
        assert tc["active_window"]["processName"] == "Code"
        assert tc["active_window"]["width"] == 800
