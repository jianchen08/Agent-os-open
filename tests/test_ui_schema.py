"""UI Schema 模块单元测试。

覆盖：
- types: Pydantic 模型创建与序列化
- parser: YAML 解析、默认值填充、热重载
- validator: 必填字段校验、API 端点格式、widget 白名单
- API 路由: GET /api/modules/ui, GET /api/modules/ui/{module_id}
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ============================================================
# types 测试
# ============================================================


class TestModuleIdentity:
    """ModuleIdentity 类型测试。"""

    def test_create_with_required_fields(self) -> None:
        """仅提供必填字段时应成功创建。"""
        from ui_schema.types import ModuleIdentity

        identity = ModuleIdentity(
            id="comfyui",
            name="ComfyUI",
            version="1.0.0",
            category="extension",
        )
        assert identity.id == "comfyui"
        assert identity.name == "ComfyUI"
        assert identity.version == "1.0.0"
        assert identity.category == "extension"
        assert identity.description is None
        assert identity.tags is None

    def test_create_with_all_fields(self) -> None:
        """提供所有字段时应成功创建。"""
        from ui_schema.types import ModuleIdentity

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

    def test_category_literal(self) -> None:
        """category 应只接受合法值。"""
        from ui_schema.types import ModuleIdentity

        for valid in ("builtin", "extension", "custom"):
            identity = ModuleIdentity(
                id="test", name="Test", version="1.0.0", category=valid
            )
            assert identity.category == valid

    def test_missing_required_field_raises(self) -> None:
        """缺少必填字段 id 应抛出验证错误。"""
        from pydantic import ValidationError

        from ui_schema.types import ModuleIdentity

        with pytest.raises(ValidationError):
            ModuleIdentity(name="Test")  # 缺少必填 id


class TestModuleAction:
    """ModuleAction 类型测试。"""

    def test_create_minimal(self) -> None:
        """最小化创建应成功。"""
        from ui_schema.types import ModuleAction

        action = ModuleAction(id="generate", name="生成图片", type="command")
        assert action.id == "generate"
        assert action.type == "command"
        assert action.requires_confirmation is False
        assert action.is_dangerous is False

    def test_type_literal(self) -> None:
        """type 应只接受 command/query/event/stream。"""
        from ui_schema.types import ModuleAction

        for valid in ("command", "query", "event", "stream"):
            action = ModuleAction(id="a", name="b", type=valid)
            assert action.type == valid


class TestChatInteractionType:
    """ChatInteractionType 枚举测试。"""

    def test_all_interaction_types(self) -> None:
        """所有 8 种交互类型应可创建。"""
        from ui_schema.types import ChatInteractionConfig

        for itype in (
            "form", "chart", "gallery", "table",
            "progress", "code_block", "status_card", "decision",
        ):
            config = ChatInteractionConfig(type=itype)
            assert config.type == itype


class TestRenderingSpaceConfig:
    """RenderingSpaceConfig 类型测试。"""

    def test_create_with_layout(self) -> None:
        """创建包含布局配置的渲染空间。"""
        from ui_schema.types import RenderingSpaceConfig

        space = RenderingSpaceConfig(
            space="workspace",
            widget="kanban",
            layout={"width": "100%", "height": 600, "resizable": True},
        )
        assert space.space == "workspace"
        assert space.widget == "kanban"
        assert space.layout is not None
        assert space.layout["resizable"] is True


class TestModuleRendering:
    """ModuleRendering 类型测试。"""

    def test_default_chat_and_spaces(self) -> None:
        """默认 chat 和 spaces 应为空列表。"""
        from ui_schema.types import ModuleRendering

        rendering = ModuleRendering()
        assert rendering.chat == []
        assert rendering.spaces == []


class TestClientCapabilities:
    """ClientCapabilities 类型测试。"""

    def test_create_with_fallback(self) -> None:
        """创建包含降级方案的客户端能力。"""
        from ui_schema.types import ClientCapabilities

        caps = ClientCapabilities(
            required_spaces=["chat", "workspace"],
            required_widgets=["kanban", "chart"],
            fallback={"widget": "status_card", "space": "chat"},
        )
        assert caps.fallback is not None
        assert caps.fallback["widget"] == "status_card"


class TestModuleUISchema:
    """ModuleUISchema 完整类型测试。"""

    def test_create_full_schema(self) -> None:
        """创建完整的 UI Schema。"""
        from ui_schema.types import (
            ClientCapabilities,
            ModuleAction,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="test", name="Test", version="1.0.0", category="builtin"
            ),
            actions=[
                ModuleAction(id="run", name="运行", type="command"),
            ],
            rendering=ModuleRendering(),
            clients=ClientCapabilities(
                required_spaces=["chat"],
                required_widgets=[],
            ),
        )
        assert schema.identity.id == "test"
        assert len(schema.actions) == 1
        assert schema.rendering.chat == []

    def test_json_serialization(self) -> None:
        """Schema 应能正确序列化为 JSON。"""
        from ui_schema.types import (
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="ser", name="Ser", version="1.0.0", category="custom"
            ),
            actions=[],
            rendering=ModuleRendering(),
            clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
        )
        json_str = schema.model_dump_json()
        data = json.loads(json_str)
        assert data["identity"]["id"] == "ser"


# ============================================================
# parser 测试
# ============================================================


class TestSchemaParser:
    """Schema 解析器测试。"""

    def _write_yaml(self, tmp_dir: Path, filename: str, content: dict) -> Path:
        """辅助：写入 YAML 文件。"""
        filepath = tmp_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(content, f, allow_unicode=True)
        return filepath

    def test_parse_yaml_with_ui_section(self) -> None:
        """解析包含 ui 部分的 YAML 配置。"""
        from ui_schema.parser import SchemaParser

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(tmp_dir, "test_module.yaml", {
                "config_id": "test_module",
                "name": "测试模块",
                "ui": {
                    "identity": {
                        "id": "test_module",
                        "name": "测试模块",
                        "version": "1.0.0",
                        "category": "builtin",
                    },
                    "actions": [
                        {"id": "run", "name": "运行", "type": "command"},
                    ],
                    "rendering": {
                        "chat": [],
                        "spaces": [],
                    },
                    "clients": {
                        "required_spaces": ["chat"],
                        "required_widgets": [],
                    },
                },
            })
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 1
            assert schemas[0].identity.id == "test_module"

    def test_parse_yaml_without_ui_section(self) -> None:
        """不包含 ui 部分的 YAML 应被跳过。"""
        from ui_schema.parser import SchemaParser

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(tmp_dir, "no_ui.yaml", {
                "config_id": "no_ui",
                "name": "无 UI 模块",
            })
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 0

    def test_default_values_filled(self) -> None:
        """缺少的可选字段应自动填充默认值。"""
        from ui_schema.parser import SchemaParser

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(tmp_dir, "minimal.yaml", {
                "config_id": "minimal",
                "name": "最小化",
                "ui": {
                    "identity": {
                        "id": "minimal",
                        "name": "最小化",
                        "version": "0.1.0",
                        "category": "custom",
                    },
                },
            })
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 1
            # actions 和 rendering 应有默认值
            assert schemas[0].actions == []
            assert schemas[0].rendering.chat == []

    def test_load_single_file(self) -> None:
        """加载单个 YAML 文件。"""
        from ui_schema.parser import SchemaParser

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            filepath = self._write_yaml(tmp_dir, "single.yaml", {
                "config_id": "single",
                "name": "单文件",
                "ui": {
                    "identity": {
                        "id": "single",
                        "name": "单文件",
                        "version": "1.0.0",
                        "category": "extension",
                    },
                },
            })
            parser = SchemaParser()
            schema = parser.load_file(filepath)
            assert schema is not None
            assert schema.identity.id == "single"

    def test_load_file_not_found(self) -> None:
        """加载不存在的文件应返回 None。"""
        from ui_schema.parser import SchemaParser

        parser = SchemaParser()
        result = parser.load_file(Path("/nonexistent/file.yaml"))
        assert result is None

    def test_hot_reload_detects_changes(self) -> None:
        """热重载应检测到文件变更。"""
        import time

        from ui_schema.parser import SchemaParser

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._write_yaml(tmp_dir, "hot.yaml", {
                "config_id": "hot",
                "name": "热重载",
                "ui": {
                    "identity": {
                        "id": "hot",
                        "name": "热重载",
                        "version": "1.0.0",
                        "category": "builtin",
                    },
                },
            })
            parser = SchemaParser()
            schemas = parser.load_directory(tmp_dir)
            assert len(schemas) == 1

            # 修改文件
            time.sleep(0.1)
            self._write_yaml(tmp_dir, "hot.yaml", {
                "config_id": "hot",
                "name": "热重载更新",
                "ui": {
                    "identity": {
                        "id": "hot",
                        "name": "热重载更新",
                        "version": "2.0.0",
                        "category": "builtin",
                    },
                },
            })

            # 检测变更
            changed = parser.detect_changes(tmp_dir)
            assert "hot" in changed


# ============================================================
# validator 测试
# ============================================================


class TestSchemaValidator:
    """Schema 验证器测试。"""

    def test_valid_schema(self) -> None:
        """有效的 Schema 应通过验证。"""
        from ui_schema.types import (
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )
        from ui_schema.validator import SchemaValidator

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="valid", name="Valid", version="1.0.0", category="builtin"
            ),
            actions=[],
            rendering=ModuleRendering(),
            clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
        )
        validator = SchemaValidator()
        errors = validator.validate(schema)
        assert errors == []

    def test_missing_identity_name(self) -> None:
        """identity.name 为空应报告错误。"""
        from ui_schema.types import (
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )
        from ui_schema.validator import SchemaValidator

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="test", name="", version="1.0.0", category="builtin"
            ),
            actions=[],
            rendering=ModuleRendering(),
            clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
        )
        validator = SchemaValidator()
        errors = validator.validate(schema)
        assert any("identity.name" in e for e in errors)

    def test_invalid_api_endpoint_format(self) -> None:
        """action 中 API 端点格式不正确应报告错误。"""
        from ui_schema.types import (
            ClientCapabilities,
            ModuleAction,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )
        from ui_schema.validator import SchemaValidator

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="test", name="Test", version="1.0.0", category="builtin"
            ),
            actions=[
                ModuleAction(
                    id="bad_api",
                    name="坏 API",
                    type="command",
                    api="not-a-valid-endpoint",
                ),
            ],
            rendering=ModuleRendering(),
            clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
        )
        validator = SchemaValidator()
        errors = validator.validate(schema)
        assert any("api" in e.lower() for e in errors)

    def test_invalid_widget_type(self) -> None:
        """rendering 中 widget 不在白名单应报告错误。"""
        from ui_schema.types import (
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
            RenderingSpaceConfig,
        )
        from ui_schema.validator import SchemaValidator

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="test", name="Test", version="1.0.0", category="builtin"
            ),
            actions=[],
            rendering=ModuleRendering(
                spaces=[
                    RenderingSpaceConfig(
                        space="workspace",
                        widget="nonexistent_widget_xyz",
                    ),
                ],
            ),
            clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
        )
        validator = SchemaValidator()
        errors = validator.validate(schema)
        assert any("widget" in e.lower() for e in errors)

    def test_missing_identity_id(self) -> None:
        """identity.id 为空应报告错误。"""
        from ui_schema.types import (
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )
        from ui_schema.validator import SchemaValidator

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="", name="Test", version="1.0.0", category="builtin"
            ),
            actions=[],
            rendering=ModuleRendering(),
            clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
        )
        validator = SchemaValidator()
        errors = validator.validate(schema)
        assert any("identity.id" in e for e in errors)


# ============================================================
# API 路由测试
# ============================================================


class TestUIRoutes:
    """UI Schema API 路由测试。"""

    def _get_test_yaml_content(self) -> dict:
        """返回测试用的完整 YAML 内容。"""
        return {
            "config_id": "test_route_module",
            "name": "路由测试模块",
            "is_active": True,
            "ui": {
                "identity": {
                    "id": "test_route_module",
                    "name": "路由测试模块",
                    "version": "1.0.0",
                    "category": "builtin",
                    "description": "用于路由测试的模块",
                },
                "actions": [
                    {"id": "run", "name": "运行", "type": "command"},
                ],
                "rendering": {
                    "chat": [{"type": "form"}],
                    "spaces": [],
                },
                "clients": {
                    "required_spaces": ["chat"],
                    "required_widgets": ["form"],
                },
            },
        }

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_list_ui_schemas(self, mock_get_parser: MagicMock) -> None:
        """GET /api/modules/ui 返回所有 UI Schema。"""
        from fastapi.testclient import TestClient

        from ui_schema.types import (
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )

        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [
            ModuleUISchema(
                identity=ModuleIdentity(
                    id="mod1", name="Mod1", version="1.0.0", category="builtin"
                ),
                actions=[],
                rendering=ModuleRendering(),
                clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
            ),
        ]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app

        app = create_app()
        client = TestClient(app)

        # 先登录获取 token
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"username": "demo", "password": "demo12345"},
        )
        token = login_resp.json()["access_token"]

        resp = client.get(
            "/api/modules/ui",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] >= 1

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_get_single_ui_schema(self, mock_get_parser: MagicMock) -> None:
        """GET /api/modules/ui/{module_id} 返回指定模块。"""
        from fastapi.testclient import TestClient

        from ui_schema.types import (
            ClientCapabilities,
            ModuleIdentity,
            ModuleRendering,
            ModuleUISchema,
        )

        schema = ModuleUISchema(
            identity=ModuleIdentity(
                id="mod1", name="Mod1", version="1.0.0", category="builtin"
            ),
            actions=[],
            rendering=ModuleRendering(),
            clients=ClientCapabilities(required_spaces=[], required_widgets=[]),
        )

        mock_parser = MagicMock()
        mock_parser.get_schema.return_value = schema
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app

        app = create_app()
        client = TestClient(app)

        login_resp = client.post(
            "/api/v1/auth/login",
            json={"username": "demo", "password": "demo12345"},
        )
        token = login_resp.json()["access_token"]

        resp = client.get(
            "/api/modules/ui/mod1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["identity"]["id"] == "mod1"

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_get_nonexistent_module(self, mock_get_parser: MagicMock) -> None:
        """GET /api/modules/ui/{module_id} 对不存在模块返回 404。"""
        from fastapi.testclient import TestClient

        mock_parser = MagicMock()
        mock_parser.get_schema.return_value = None
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app

        app = create_app()
        client = TestClient(app)

        login_resp = client.post(
            "/api/v1/auth/login",
            json={"username": "demo", "password": "demo12345"},
        )
        token = login_resp.json()["access_token"]

        resp = client.get(
            "/api/modules/ui/nonexistent",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
