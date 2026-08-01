"""Datasource 路由测试。

覆盖 P1 API 对齐：前端 fetchDynamicDataSource 调用 GET /api/v1/datasource/{uri}，
后端此前无此路由（返回 404），需补齐。

前端期望（frontend/src/services/api/datasource.ts + types/schema.ts）：
- 2xx 成功：{ success: boolean, options?: [...] }
- 4xx 未认证：401
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from channels.api.deps import require_auth
from channels.api.routes_missing import datasource_router


def _mock_auth() -> dict:
    """覆盖认证依赖，模拟已登录用户。"""
    return {"sub": "test_user", "username": "tester"}


class TestDatasourceRoutes:
    """GET /api/v1/datasource/{uri} 路由测试。"""

    def setup_method(self) -> None:
        app = FastAPI()
        app.dependency_overrides[require_auth] = _mock_auth
        app.include_router(datasource_router)
        self.client = TestClient(app)

    def test_get_datasource_success(self) -> None:
        """2xx：GET /api/v1/datasource/categories/list 应返回 success 字段（占位响应）。"""
        resp = self.client.get("/api/v1/datasource/categories/list")
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data
        assert isinstance(data["success"], bool)

    def test_get_datasource_multi_segment_uri(self) -> None:
        """2xx：多段 uri（tools/list）也能命中路由。"""
        resp = self.client.get("/api/v1/datasource/tools/list")
        assert resp.status_code == 200
        assert "success" in resp.json()

    def test_get_datasource_unauthorized(self) -> None:
        """4xx：未认证访问返回 401。"""
        # 不带 dependency_overrides 的应用（require_auth 会拒绝）
        app = FastAPI()
        app.include_router(datasource_router)
        client = TestClient(app)
        resp = client.get("/api/v1/datasource/categories/list")
        assert resp.status_code == 401
