"""数据声明自动 CRUD 接口测试。

验证模块 YAML 中 data 声明自动暴露的 6 个 REST 接口。
对应需求：F-UI-27, F-UI-28, F-UI-29, AC-UI-10

覆盖内容：
1. GET 列表（含筛选/排序/分页）
2. GET 单条
3. POST 创建
4. PUT 更新
5. DELETE 删除
6. 路由前缀正确（/api/v1/modules/{module_id}/data/{collection}）

使用 dependency_overrides 绕过 auth 依赖，避免外部数据库依赖。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CRUD_DEFINITION: dict[str, Any] = {
    "fields": {
        "id": {"type": "uuid", "primary": True, "auto": True},
        "name": {"type": "string", "required": True},
        "type": {"type": "enum", "values": ["weapon", "armor", "potion"]},
        "quantity": {"type": "integer", "default": 1, "min": 0},
    },
    "access": "crud",
    "filters": ["type"],
    "sort": ["name", "quantity"],
    "pagination": True,
}


def _fake_auth():
    """模拟认证依赖，返回固定用户信息。"""
    async def _dep():
        return {"user_id": "test-user", "username": "tester", "role": "admin"}
    return _dep


def _register_crud_exception_handler(app: FastAPI) -> None:
    """注册 AutoCRUDError 异常处理器，使其返回正确的 HTTP 状态码。"""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from ui_schema.auth_types import AutoCRUDError

    @app.exception_handler(AutoCRUDError)
    async def _handle_crud_error(request: Request, exc: AutoCRUDError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {
                    "code": exc.error_code,
                    "message": exc.message,
                    "details": exc.details,
                },
            },
        )


@pytest.fixture
def app_and_client():
    """创建带 CRUD 路由的 FastAPI 应用（auth 已绕过）。"""
    _clear_store()
    app = FastAPI()

    generator = AutoCRUDGenerator()
    router = generator.register("test_mod", "items", CRUD_DEFINITION)
    assert router is not None
    app.include_router(router)

    # 注册 AutoCRUDError 异常处理器
    _register_crud_exception_handler(app)

    # 绕过 auth 依赖
    from channels.api.deps import require_auth
    app.dependency_overrides[require_auth] = _fake_auth()

    client = TestClient(app)
    yield app, client

    _clear_store()
    app.dependency_overrides.clear()


# ===========================================================================
# 一、6 个 REST 接口存在性验证
# ===========================================================================


class TestCRUDEndpointsExist:
    """验证 CRUD 路由正确暴露 6 个 REST 接口。

    验证点（F-UI-27, AC-UI-10）：声明 data → 6 个接口可用。
    """

    def test_get_list_endpoint_exists(self, app_and_client) -> None:
        """GET /api/v1/modules/{module}/data/{collection} — 列表查询。"""
        _, client = app_and_client
        resp = client.get("/api/v1/modules/test_mod/data/items")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_get_single_endpoint_exists(self, app_and_client) -> None:
        """GET /api/v1/modules/{module}/data/{collection}/{id} — 单条查询。"""
        _, client = app_and_client
        # 先创建一条记录
        create_resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Sword", "type": "weapon", "quantity": 1},
        )
        assert create_resp.status_code == 200
        record_id = create_resp.json()["id"]

        # 查询单条
        resp = client.get(f"/api/v1/modules/test_mod/data/items/{record_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == record_id

    def test_post_create_endpoint_exists(self, app_and_client) -> None:
        """POST /api/v1/modules/{module}/data/{collection} — 创建。"""
        _, client = app_and_client
        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Shield", "type": "armor", "quantity": 2},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Shield"
        assert "id" in data

    def test_put_update_endpoint_exists(self, app_and_client) -> None:
        """PUT /api/v1/modules/{module}/data/{collection}/{id} — 更新。"""
        _, client = app_and_client
        create_resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Potion", "type": "potion", "quantity": 5},
        )
        record_id = create_resp.json()["id"]

        resp = client.put(
            f"/api/v1/modules/test_mod/data/items/{record_id}",
            json={"quantity": 10},
        )
        assert resp.status_code == 200
        assert resp.json()["quantity"] == 10

    def test_delete_endpoint_exists(self, app_and_client) -> None:
        """DELETE /api/v1/modules/{module}/data/{collection}/{id} — 删除。"""
        _, client = app_and_client
        create_resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Temp", "type": "potion", "quantity": 1},
        )
        record_id = create_resp.json()["id"]

        resp = client.delete(f"/api/v1/modules/test_mod/data/items/{record_id}")
        assert resp.status_code == 200

        # 确认已删除
        get_resp = client.get(f"/api/v1/modules/test_mod/data/items/{record_id}")
        assert get_resp.status_code == 404


# ===========================================================================
# 二、CRUD 全流程验证
# ===========================================================================


class TestCRUDFullFlow:
    """CRUD 全流程：创建 → 读取 → 更新 → 删除。

    验证点（AC-UI-10）：声明 data → 6 个接口可用。
    """

    def test_create_read_update_delete_cycle(self, app_and_client) -> None:
        """完整 CRUD 周期：POST → GET → PUT → DELETE。"""
        _, client = app_and_client

        # 1. POST 创建
        create_resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "TestItem", "type": "weapon", "quantity": 3},
        )
        assert create_resp.status_code == 200
        item = create_resp.json()
        record_id = item["id"]

        # 2. GET 单条
        get_resp = client.get(f"/api/v1/modules/test_mod/data/items/{record_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == "TestItem"

        # 3. PUT 更新
        put_resp = client.put(
            f"/api/v1/modules/test_mod/data/items/{record_id}",
            json={"name": "UpdatedItem", "quantity": 99},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["name"] == "UpdatedItem"
        assert put_resp.json()["quantity"] == 99

        # 4. GET 列表验证
        list_resp = client.get("/api/v1/modules/test_mod/data/items")
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] >= 1

        # 5. DELETE 删除
        del_resp = client.delete(f"/api/v1/modules/test_mod/data/items/{record_id}")
        assert del_resp.status_code == 200

        # 6. 确认已删除
        get_after = client.get(f"/api/v1/modules/test_mod/data/items/{record_id}")
        assert get_after.status_code == 404


# ===========================================================================
# 三、数据校验
# ===========================================================================


class TestCRUDValidation:
    """CRUD 数据校验。

    验证点（F-UI-29）：access 控制 + 筛选白名单 + 必填校验。
    """

    def test_create_missing_required_field(self, app_and_client) -> None:
        """缺少必填字段 → 返回错误。"""
        _, client = app_and_client
        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"type": "weapon", "quantity": 1},  # 缺少 name
        )
        assert resp.status_code in (400, 422)

    def test_create_invalid_enum_value(self, app_and_client) -> None:
        """枚举值不在允许范围 → 返回错误。"""
        _, client = app_and_client
        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Invalid", "type": "invalid_type", "quantity": 1},
        )
        assert resp.status_code in (400, 422)

    def test_create_with_default_value(self, app_and_client) -> None:
        """未提供的字段使用默认值。"""
        _, client = app_and_client
        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "DefaultItem"},  # quantity 未提供
        )
        assert resp.status_code == 200
        assert resp.json()["quantity"] == 1  # 默认值


# ===========================================================================
# 四、筛选、排序、分页
# ===========================================================================


class TestCRUDQueryFeatures:
    """CRUD 查询功能：筛选、排序、分页。

    验证点（F-UI-28, F-UI-29）。
    """

    def test_filter_by_field(self, app_and_client) -> None:
        """按字段筛选结果。"""
        _, client = app_and_client
        client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "A", "type": "weapon", "quantity": 1},
        )
        client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "B", "type": "armor", "quantity": 2},
        )

        resp = client.get("/api/v1/modules/test_mod/data/items?type=weapon")
        assert resp.status_code == 200
        data = resp.json()
        assert all(r["type"] == "weapon" for r in data["items"])

    def test_pagination(self, app_and_client) -> None:
        """分页返回正确的元数据。"""
        _, client = app_and_client
        for i in range(5):
            client.post(
                "/api/v1/modules/test_mod/data/items",
                json={"name": f"Item{i}", "type": "potion", "quantity": i},
            )

        resp = client.get(
            "/api/v1/modules/test_mod/data/items?_page=1&_page_size=2"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) <= 2
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["page_size"] == 2


# ===========================================================================
# 五、路由前缀验证
# ===========================================================================


class TestCRUDRoutePrefix:
    """路由前缀符合协议规范。

    验证点（需求 §3.2, AC-UI-10）：路径为 /api/v1/modules/{module}/data/{collection}。
    """

    def test_router_prefix_correct(self) -> None:
        """注册的路由前缀符合 API 契约。"""
        _clear_store()
        generator = AutoCRUDGenerator()
        router = generator.register("my_module", "my_collection", CRUD_DEFINITION)
        assert router is not None
        assert router.prefix == "/api/v1/modules/my_module/data/my_collection"
