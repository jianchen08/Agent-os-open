"""UI Schema 类型定义测试。

覆盖：
- Pydantic 模型序列化/反序列化
- 字段别名（alias）正确映射
- 必填字段验证
- 默认值填充
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

# ============================================================
# ModuleIdentity 测试
# ============================================================


class TestModuleIdentity:
    """ModuleIdentity 类型测试。"""

    def test_create_with_required_fields_only(self) -> None:
        """仅提供必填字段 id 和 name 时应成功创建。"""
        from ui_schema.types import ModuleIdentity  # noqa: PLC0415

        identity = ModuleIdentity(id="test", name="测试模块")
        assert identity.id == "test"
        assert identity.name == "测试模块"

    def test_default_values_filled(self) -> None:
        """未提供的可选字段应自动填充默认值。"""
        from ui_schema.types import ModuleIdentity  # noqa: PLC0415

        identity = ModuleIdentity(id="test", name="测试")
        assert identity.version == "1.0.0"
        assert identity.category == "custom"
        assert identity.description is None
        assert identity.icon is None
        assert identity.author is None
        assert identity.tags is None

    def test_create_with_all_fields(self) -> None:
        """提供所有字段时应成功创建。"""
        from ui_schema.types import ModuleIdentity  # noqa: PLC0415

        identity = ModuleIdentity(
            id="pm",
            name="项目管理",
            version="2.0.0",
            category="builtin",
            description="项目管理模块",
            icon="📊",
            author="team",
            tags=["management", "tasks"],
        )
        assert identity.icon == "📊"
        assert identity.author == "team"
        assert identity.tags == ["management", "tasks"]

    def test_category_literal_values(self) -> None:
        """category 应接受 builtin/extension/custom 三种合法值。"""
        from ui_schema.types import ModuleIdentity  # noqa: PLC0415

        for valid in ("builtin", "extension", "custom"):
            identity = ModuleIdentity(id="t", name="t", category=valid)
            assert identity.category == valid

    def test_missing_id_raises_validation_error(self) -> None:
        """缺少必填字段 id 应抛出 ValidationError。"""
        from ui_schema.types import ModuleIdentity  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ModuleIdentity(name="Test")

    def test_missing_name_raises_validation_error(self) -> None:
        """缺少必填字段 name 应抛出 ValidationError。"""
        from ui_schema.types import ModuleIdentity  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ModuleIdentity(id="test")

    def test_serialization_round_trip(self) -> None:
        """序列化后反序列化应保持数据一致。"""
        from ui_schema.types import ModuleIdentity  # noqa: PLC0415

        original = ModuleIdentity(
            id="round_trip",
            name="往返测试",
            version="3.0.0",
            category="extension",
            description="测试序列化",
            icon="🔄",
            author="tester",
            tags=["test"],
        )
        json_str = original.model_dump_json()
        data = json.loads(json_str)
        restored = ModuleIdentity(**data)
        assert restored == original


# ============================================================
# ModuleAction 测试
# ============================================================


class TestModuleAction:
    """ModuleAction 类型测试。"""

    def test_create_minimal(self) -> None:
        """最小化创建应成功。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        action = ModuleAction(id="generate", name="生成图片")
        assert action.id == "generate"
        assert action.name == "生成图片"
        assert action.type == "command"
        assert action.requires_confirmation is False
        assert action.is_dangerous is False
        assert action.description is None
        assert action.input_schema is None
        assert action.output_schema is None
        assert action.api is None
        assert action.params is None
        assert action.label is None

    def test_type_literal_values(self) -> None:
        """type 应接受 command/query/event/stream 四种合法值。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        for valid in ("command", "query", "event", "stream"):
            action = ModuleAction(id="a", name="b", type=valid)
            assert action.type == valid

    def test_alias_mapping_requiresConfirmation(self) -> None:  # noqa: N802
        """requiresConfirmation 别名映射正确。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        action = ModuleAction(
            id="test",
            name="test",
            requiresConfirmation=True,
        )
        assert action.requires_confirmation is True

        # 通过别名构造
        action2 = ModuleAction(
            id="test2",
            name="test2",
            requiresConfirmation=True,
        )
        assert action2.requires_confirmation is True

    def test_alias_mapping_isDangerous(self) -> None:  # noqa: N802
        """isDangerous 别名映射正确。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        action = ModuleAction(id="t", name="t", isDangerous=True)
        assert action.is_dangerous is True

    def test_alias_mapping_inputSchema(self) -> None:  # noqa: N802
        """inputSchema 别名映射正确。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        schema_def = {"type": "object", "properties": {"name": {"type": "string"}}}
        action = ModuleAction(id="t", name="t", inputSchema=schema_def)
        assert action.input_schema == schema_def

    def test_alias_mapping_outputSchema(self) -> None:  # noqa: N802
        """outputSchema 别名映射正确。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        schema_def = {"type": "array"}
        action = ModuleAction(id="t", name="t", outputSchema=schema_def)
        assert action.output_schema == schema_def

    def test_serialization_by_alias(self) -> None:
        """model_dump(by_alias=True) 应输出驼峰命名。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        action = ModuleAction(
            id="t",
            name="t",
            requiresConfirmation=True,
            isDangerous=True,
            inputSchema={"type": "object"},
            outputSchema={"type": "string"},
        )
        data = action.model_dump(by_alias=True, exclude_none=True)
        assert "requiresConfirmation" in data
        assert "isDangerous" in data
        assert "inputSchema" in data
        assert "outputSchema" in data

    def test_missing_id_raises_validation_error(self) -> None:
        """缺少必填字段 id 应抛出 ValidationError。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ModuleAction(name="test")

    def test_missing_name_raises_validation_error(self) -> None:
        """缺少必填字段 name 应抛出 ValidationError。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ModuleAction(id="test")


# ============================================================
# ChatInteractionConfig 测试
# ============================================================


class TestChatInteractionConfig:
    """ChatInteractionConfig 类型测试。"""

    def test_all_interaction_types(self) -> None:
        """所有 8 种交互类型应可创建。"""
        from ui_schema.types import ChatInteractionConfig  # noqa: PLC0415

        for itype in (
            "form",
            "chart",
            "gallery",
            "table",
            "progress",
            "code_block",
            "status_card",
            "decision",
        ):
            config = ChatInteractionConfig(type=itype)
            assert config.type == itype

    def test_alias_dataSource(self) -> None:  # noqa: N802
        """dataSource 别名映射正确。"""
        from ui_schema.types import ChatInteractionConfig  # noqa: PLC0415

        config = ChatInteractionConfig(type="form", dataSource="module://items")
        assert config.data_source == "module://items"

    def test_alias_refreshInterval(self) -> None:  # noqa: N802
        """refreshInterval 别名映射正确。"""
        from ui_schema.types import ChatInteractionConfig  # noqa: PLC0415

        config = ChatInteractionConfig(type="chart", refreshInterval=30000)
        assert config.refresh_interval == 30000

    def test_default_values(self) -> None:
        """可选字段默认值应为 None。"""
        from ui_schema.types import ChatInteractionConfig  # noqa: PLC0415

        config = ChatInteractionConfig(type="form")
        assert config.props is None
        assert config.data_source is None
        assert config.refresh_interval is None

    def test_missing_type_raises_validation_error(self) -> None:
        """缺少必填字段 type 应抛出 ValidationError。"""
        from ui_schema.types import ChatInteractionConfig  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ChatInteractionConfig()


# ============================================================
# LayoutConfig 测试
# ============================================================


class TestLayoutConfig:
    """LayoutConfig 类型测试。"""

    def test_alias_minWidth_minHeight(self) -> None:  # noqa: N802
        """minWidth/minHeight 别名映射正确。"""
        from ui_schema.types import LayoutConfig  # noqa: PLC0415

        layout = LayoutConfig(minWidth=100, minHeight=50)
        assert layout.min_width == 100
        assert layout.min_height == 50

    def test_all_fields_none_by_default(self) -> None:
        """所有字段默认值应为 None。"""
        from ui_schema.types import LayoutConfig  # noqa: PLC0415

        layout = LayoutConfig()
        assert layout.width is None
        assert layout.height is None
        assert layout.min_width is None
        assert layout.min_height is None
        assert layout.resizable is None
        assert layout.draggable is None
        assert layout.position is None


# ============================================================
# RenderingSpaceConfig 测试
# ============================================================


class TestRenderingSpaceConfig:
    """RenderingSpaceConfig 类型测试。"""

    def test_create_with_all_fields(self) -> None:
        """创建包含所有字段的渲染空间配置。"""
        from ui_schema.types import RenderingSpaceConfig  # noqa: PLC0415

        space = RenderingSpaceConfig(
            space="workspace",
            widget="kanban",
            props={"key": "value"},
            dataSource="module://items",
            layout={"width": "100%", "height": 600},
            autoOpen={"event": "on_task_start", "delay": 500},
        )
        assert space.space == "workspace"
        assert space.widget == "kanban"
        assert space.data_source == "module://items"
        assert space.auto_open is not None
        assert space.auto_open["event"] == "on_task_start"

    def test_default_values(self) -> None:
        """默认 space 为 workspace，widget 为空字符串。"""
        from ui_schema.types import RenderingSpaceConfig  # noqa: PLC0415

        space = RenderingSpaceConfig()
        assert space.space == "workspace"
        assert space.widget == ""
        assert space.props is None
        assert space.data_source is None
        assert space.layout is None
        assert space.auto_open is None


# ============================================================
# DockConfig 测试
# ============================================================


class TestDockConfig:
    """DockConfig 类型测试。"""

    def test_default_values(self) -> None:
        """默认值正确。"""
        from ui_schema.types import DockConfig  # noqa: PLC0415

        dock = DockConfig()
        assert dock.icon is None
        assert dock.label is None
        assert dock.indicator == "none"
        assert dock.indicator_color is None

    def test_alias_indicatorColor(self) -> None:  # noqa: N802
        """indicatorColor 别名映射正确。"""
        from ui_schema.types import DockConfig  # noqa: PLC0415

        dock = DockConfig(indicatorColor="#52c41a")
        assert dock.indicator_color == "#52c41a"


# ============================================================
# FullscreenConfig 测试
# ============================================================


class TestFullscreenConfig:
    """FullscreenConfig 类型测试。"""

    def test_alias_triggerEvent_autoEnter(self) -> None:  # noqa: N802
        """别名映射正确。"""
        from ui_schema.types import FullscreenConfig  # noqa: PLC0415

        config = FullscreenConfig(triggerEvent="on_full_edit", autoEnter=True)
        assert config.trigger_event == "on_full_edit"
        assert config.auto_enter is True


# ============================================================
# ModuleRendering 测试
# ============================================================


class TestModuleRendering:
    """ModuleRendering 类型测试。"""

    def test_default_chat_and_spaces_empty(self) -> None:
        """默认 chat 和 spaces 应为空列表。"""
        from ui_schema.types import ModuleRendering  # noqa: PLC0415

        rendering = ModuleRendering()
        assert rendering.chat == []
        assert rendering.spaces == []
        assert rendering.dock is None
        assert rendering.fullscreen is None


# ============================================================
# ClientCapabilities 测试
# ============================================================


class TestClientCapabilities:
    """ClientCapabilities 类型测试。"""

    def test_default_values(self) -> None:
        """默认 required_spaces 和 required_widgets 为空列表。"""
        from ui_schema.types import ClientCapabilities  # noqa: PLC0415

        caps = ClientCapabilities()
        assert caps.required_spaces == []
        assert caps.required_widgets == []
        assert caps.min_client_version is None
        assert caps.fallback is None

    def test_create_with_fallback(self) -> None:
        """创建包含降级方案的客户端能力。"""
        from ui_schema.types import ClientCapabilities  # noqa: PLC0415

        caps = ClientCapabilities(
            required_spaces=["chat", "workspace"],
            required_widgets=["kanban", "chart"],
            minClientVersion="1.0.0",
            fallback={"widget": "status_card", "space": "chat"},
        )
        assert caps.fallback is not None
        assert caps.fallback["widget"] == "status_card"
        assert caps.min_client_version == "1.0.0"

    def test_alias_minClientVersion(self) -> None:  # noqa: N802
        """minClientVersion 别名映射正确。"""
        from ui_schema.types import ClientCapabilities  # noqa: PLC0415

        caps = ClientCapabilities(minClientVersion="2.0.0")
        assert caps.min_client_version == "2.0.0"


# ============================================================
# ModuleUISchema 完整测试
# ============================================================


class TestModuleUISchema:
    """ModuleUISchema 完整类型测试。"""

    def _make_schema(self, **kwargs) -> ModuleUISchema:  # noqa: F821
        """创建测试用 Schema。"""
        from ui_schema.types import (  # noqa: PLC0415
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )

        defaults = {
            "identity": ModuleIdentity(id="test", name="Test", version="1.0.0"),
            "actions": [],
            "rendering": ModuleRendering(),
            "clients": ClientCapabilities(),
        }
        defaults.update(kwargs)
        return ModuleUISchema(**defaults)

    def test_create_full_schema(self) -> None:
        """创建完整的 UI Schema。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        schema = self._make_schema(
            actions=[ModuleAction(id="run", name="运行", type="command")],
        )
        assert schema.identity.id == "test"
        assert len(schema.actions) == 1
        assert schema.rendering.chat == []
        assert schema.clients.required_spaces == []

    def test_default_actions_rendering_clients(self) -> None:
        """不提供 actions/rendering/clients 时应使用默认值。"""
        from ui_schema.types import ModuleUISchema  # noqa: PLC0415

        schema = ModuleUISchema(
            identity={"id": "minimal", "name": "Minimal"},
        )
        assert schema.actions == []
        assert schema.rendering.chat == []
        assert schema.clients.required_spaces == []

    def test_json_serialization_round_trip(self) -> None:
        """Schema 应能正确序列化为 JSON 并反序列化。"""
        schema = self._make_schema()
        json_str = schema.model_dump_json()
        data = json.loads(json_str)
        assert data["identity"]["id"] == "test"

    def test_serialization_by_alias(self) -> None:
        """model_dump(by_alias=True) 输出前端兼容的驼峰命名。"""
        from ui_schema.types import ModuleAction  # noqa: PLC0415

        schema = self._make_schema(
            actions=[
                ModuleAction(
                    id="a",
                    name="a",
                    requiresConfirmation=True,
                    isDangerous=True,
                ),
            ],
        )
        data = schema.model_dump(by_alias=True, exclude_none=True)
        action = data["actions"][0]
        assert "requiresConfirmation" in action
        assert "isDangerous" in action

    def test_missing_identity_raises_validation_error(self) -> None:
        """缺少必填字段 identity 应抛出 ValidationError。"""
        from ui_schema.types import ModuleUISchema  # noqa: PLC0415

        with pytest.raises(ValidationError):
            ModuleUISchema()
