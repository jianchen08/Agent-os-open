"""UI Schema API 路由测试。

覆盖：
- GET /api/v1/modules/ui 返回 Schema 列表
- GET /api/v1/modules/ui/{module_id} 返回指定模块 Schema
- module_id 不存在返回 404
- client_type 过滤参数功能
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ui_schema.types import (
    ClientCapabilities,
    ModuleIdentity,
    ModuleRendering,
    ModuleUISchema,
    RenderingSpaceConfig,
)


def _make_schema(
    module_id: str = "test_mod",
    name: str = "Test",
    spaces: list[RenderingSpaceConfig] | None = None,
    required_spaces: list[str] | None = None,
    dock: dict | None = None,
) -> ModuleUISchema:
    """创建测试用 Schema。"""
    return ModuleUISchema(
        identity=ModuleIdentity(id=module_id, name=name, version="1.0.0"),
        actions=[],
        rendering=ModuleRendering(
            spaces=spaces or [],
            dock=dock,
        ),
        clients=ClientCapabilities(
            required_spaces=required_spaces or [],
            required_widgets=[],
        ),
    )


def _get_auth_token(client) -> str:
    """获取认证 token 的辅助函数。"""
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "demo", "password": "demo12345"},
    )
    assert login_resp.status_code == 200
    return login_resp.json()["access_token"]


# ============================================================
# GET /api/v1/modules/ui - 列表接口
# ============================================================


class TestListUISchemas:
    """GET /api/v1/modules/ui 测试。"""

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_list_returns_items_and_total(self, mock_get_parser: MagicMock) -> None:
        """GET /api/v1/modules/ui 返回 items 和 total。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [
            _make_schema("mod1", "Mod1"),
            _make_schema("mod2", "Mod2"),
        ]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert data["total"] == 2
        assert len(data["items"]) == 2

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_list_empty_when_no_schemas(self, mock_get_parser: MagicMock) -> None:
        """没有 Schema 时返回空列表。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = []
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_list_schema_serialization_by_alias(self, mock_get_parser: MagicMock) -> None:
        """返回的 Schema 应使用驼峰命名（by_alias）。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [_make_schema("alias-test", "Alias Test")]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["identity"]["id"] == "alias-test"


# ============================================================
# GET /api/v1/modules/ui/{module_id} - 详情接口
# ============================================================


class TestGetUISchema:
    """GET /api/v1/modules/ui/{module_id} 测试。"""

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_get_existing_module(self, mock_get_parser: MagicMock) -> None:
        """获取存在的模块应返回 200 和 Schema 数据。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        schema = _make_schema("mod1", "Mod1")
        mock_parser = MagicMock()
        mock_parser.get_schema.return_value = schema
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui/mod1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["identity"]["id"] == "mod1"
        assert data["identity"]["name"] == "Mod1"

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_get_nonexistent_module_returns_404(self, mock_get_parser: MagicMock) -> None:
        """获取不存在的模块应返回 404。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        mock_parser = MagicMock()
        mock_parser.get_schema.return_value = None
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui/nonexistent",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


# ============================================================
# client_type 过滤参数
# ============================================================


class TestClientTypeFilter:
    """client_type 过滤参数测试。"""

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_filter_by_ide_client_type(self, mock_get_parser: MagicMock) -> None:
        """IDE 客户端过滤：只保留 chat 和 workspace 空间。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        schema = _make_schema(
            "filter-mod",
            "Filter Mod",
            spaces=[
                RenderingSpaceConfig(space="workspace", widget="table"),
                RenderingSpaceConfig(space="floating", widget="chart"),
                RenderingSpaceConfig(space="dock", widget="status_card"),
            ],
        )
        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [schema]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui?client_type=ide",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        # IDE 只支持 chat 和 workspace，floating 和 dock 应被过滤
        spaces = data["items"][0]["rendering"]["spaces"]
        space_types = [s["space"] for s in spaces]
        assert "workspace" in space_types
        assert "floating" not in space_types

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_filter_by_mobile_removes_dock(self, mock_get_parser: MagicMock) -> None:
        """Mobile 客户端过滤：移除 dock 配置。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        schema = _make_schema(
            "mobile-mod",
            "Mobile Mod",
            dock={"icon": "📱", "label": "Mobile", "indicator": "dot"},
        )
        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [schema]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui?client_type=mobile",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # mobile 不支持 dock，dock 应被移除（exclude_none=True 时不包含 dock 键）
        assert data["items"][0]["rendering"].get("dock") is None

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_filter_by_unsupported_client_type_no_filter(self, mock_get_parser: MagicMock) -> None:
        """不支持的 client_type 应不做过滤。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        schema = _make_schema("unknown-ct", "Unknown CT")
        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [schema]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui?client_type=unknown_client",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_filter_by_client_type_required_spaces_check(self, mock_get_parser: MagicMock) -> None:
        """client_type 过滤时检查 required_spaces 兼容性。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        # 模块要求 dock 空间，但 IDE 不支持 dock
        schema = _make_schema(
            "req-dock",
            "Requires Dock",
            required_spaces=["chat", "dock"],
        )
        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [schema]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui?client_type=ide",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # IDE 不支持 dock，required_spaces 包含 dock 的模块应被过滤掉
        assert data["total"] == 0

    @patch("channels.api.routes_ui._get_schema_parser")
    def test_no_client_type_returns_all(self, mock_get_parser: MagicMock) -> None:
        """不传 client_type 应返回所有 Schema。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        mock_parser = MagicMock()
        mock_parser.list_schemas.return_value = [
            _make_schema("mod1"),
            _make_schema("mod2"),
            _make_schema("mod3"),
        ]
        mock_get_parser.return_value = mock_parser

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/ui",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 3


# ============================================================
# 认证测试
# ============================================================


class TestUIRoutesAuth:
    """UI Schema 路由认证测试。"""

    def test_unauthenticated_request_returns_401(self) -> None:
        """未认证请求应返回 401。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)

        resp = client.get("/api/v1/modules/ui")
        assert resp.status_code == 401

    def test_unauthenticated_get_by_id_returns_401(self) -> None:
        """未认证获取单个模块应返回 401。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        client = TestClient(app)

        resp = client.get("/api/v1/modules/ui/some_id")
        assert resp.status_code == 401
