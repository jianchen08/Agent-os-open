"""CRUD 自动生成器测试。

覆盖：
- AutoCRUDGenerator 路由注册
- CRUD 全流程（创建、读取、更新、删除）
- 数据校验（必填、类型、枚举、范围）
- 筛选、排序、分页
- access 模式控制（crud / read-only / write-only）
- SchemaParser data 段解析
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

# ============================================================
# 辅助工具
# ============================================================


def _get_auth_token(client) -> str:
    """获取认证 token 的辅助函数。"""
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "demo", "password": "demo12345"},
    )
    assert login_resp.status_code == 200
    return login_resp.json()["access_token"]


def _crud_definition() -> dict:
    """创建标准 CRUD 集合定义。"""
    return {
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


def _create_app_with_crud():
    """创建包含 CRUD 路由的测试应用。"""
    from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store  # noqa: PLC0415

    # 清空存储
    _clear_store()

    generator = AutoCRUDGenerator()
    definition = _crud_definition()
    router = generator.register("test_mod", "items", definition)
    assert router is not None

    from channels.api.app import create_app  # noqa: PLC0415

    app = create_app()
    app.include_router(router)
    return app


# ============================================================
# AutoCRUDGenerator 单元测试
# ============================================================


class TestAutoCRUDGeneratorRegister:
    """AutoCRUDGenerator.register 测试。"""

    def test_register_returns_router(self) -> None:
        """register 应返回有效的 APIRouter。"""
        from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store  # noqa: PLC0415

        _clear_store()
        generator = AutoCRUDGenerator()
        router = generator.register("mod1", "coll1", _crud_definition())
        assert router is not None
        # 检查路由前缀
        assert router.prefix == "/api/v1/modules/mod1/data/coll1"

    def test_register_invalid_fields_returns_none(self) -> None:
        """缺少 fields 的定义应返回 None。"""
        from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store  # noqa: PLC0415

        _clear_store()
        generator = AutoCRUDGenerator()
        result = generator.register("mod1", "coll1", {"access": "crud"})
        assert result is None

    def test_register_auto_adds_id_if_no_primary(self) -> None:
        """没有主键字段时应自动添加 id 字段。"""
        from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store  # noqa: PLC0415

        _clear_store()
        definition = {
            "fields": {
                "name": {"type": "string", "required": True},
            },
            "access": "crud",
        }
        generator = AutoCRUDGenerator()
        router = generator.register("mod1", "coll1", definition)
        assert router is not None

    def test_register_all_batch(self) -> None:
        """register_all 批量注册多个集合。"""
        from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store  # noqa: PLC0415

        _clear_store()
        generator = AutoCRUDGenerator()
        data_decls = {
            "items": _crud_definition(),
            "users": {
                "fields": {
                    "id": {"type": "uuid", "primary": True, "auto": True},
                    "name": {"type": "string", "required": True},
                },
                "access": "crud",
            },
        }
        routers = generator.register_all("mod1", data_decls)
        assert len(routers) == 2


# ============================================================
# CRUD 全流程集成测试
# ============================================================


class TestCRUDCreate:
    """POST 创建记录测试。"""

    def test_create_record_success(self) -> None:
        """成功创建一条记录。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Sword", "type": "weapon", "quantity": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Sword"
        assert data["type"] == "weapon"
        assert data["quantity"] == 3
        assert "id" in data
        assert "_created_at" in data

    def test_create_record_with_default_value(self) -> None:
        """未提供字段应使用默认值。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Potion"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["quantity"] == 1  # default: 1

    def test_create_record_missing_required_field(self) -> None:
        """缺少必填字段应返回 400。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"type": "weapon"},  # 缺少 name
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_create_record_invalid_enum(self) -> None:
        """枚举值不合法应返回 400。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Test", "type": "invalid_type"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_create_record_value_below_min(self) -> None:
        """数值小于最小值应返回 400。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Test", "quantity": -1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


class TestCRUDRead:
    """GET 读取记录测试。"""

    def test_list_records(self) -> None:
        """获取记录列表。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        # 先创建几条记录
        for name in ["Sword", "Shield", "Potion"]:
            client.post(
                "/api/v1/modules/test_mod/data/items",
                json={"name": name, "type": "weapon"},
                headers={"Authorization": f"Bearer {token}"},
            )

        resp = client.get(
            "/api/v1/modules/test_mod/data/items",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_get_single_record(self) -> None:
        """获取单条记录。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        # 创建记录
        create_resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Sword", "type": "weapon"},
            headers={"Authorization": f"Bearer {token}"},
        )
        record_id = create_resp.json()["id"]

        # 获取记录
        resp = client.get(
            f"/api/v1/modules/test_mod/data/items/{record_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == record_id
        assert data["name"] == "Sword"

    def test_get_nonexistent_record(self) -> None:
        """获取不存在的记录应返回 404。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.get(
            "/api/v1/modules/test_mod/data/items/nonexistent-id",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_list_with_pagination(self) -> None:
        """分页查询。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        # 创建 5 条记录
        for i in range(5):
            client.post(
                "/api/v1/modules/test_mod/data/items",
                json={"name": f"Item{i}", "type": "weapon"},
                headers={"Authorization": f"Bearer {token}"},
            )

        # 第 1 页，每页 2 条
        resp = client.get(
            "/api/v1/modules/test_mod/data/items?_page=1&_page_size=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["page"] == 1
        assert data["page_size"] == 2
        assert data["total_pages"] == 3

    def test_list_with_filter(self) -> None:
        """按字段筛选。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        # 创建不同类型的记录
        client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Sword", "type": "weapon"},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Helmet", "type": "armor"},
            headers={"Authorization": f"Bearer {token}"},
        )

        # 按 type 筛选
        resp = client.get(
            "/api/v1/modules/test_mod/data/items?type=weapon",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Sword"

    def test_list_with_sort(self) -> None:
        """排序查询。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        # 创建记录
        client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Banana", "type": "potion", "quantity": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Apple", "type": "potion", "quantity": 3},
            headers={"Authorization": f"Bearer {token}"},
        )

        # 按 name 升序
        resp = client.get(
            "/api/v1/modules/test_mod/data/items?_sort=name&_order=asc",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["name"] == "Apple"
        assert data["items"][1]["name"] == "Banana"


class TestCRUDUpdate:
    """PUT 更新记录测试。"""

    def test_update_record_success(self) -> None:
        """成功更新记录。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        # 创建记录
        create_resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Sword", "type": "weapon", "quantity": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        record_id = create_resp.json()["id"]

        # 更新记录
        resp = client.put(
            f"/api/v1/modules/test_mod/data/items/{record_id}",
            json={"quantity": 10, "name": "Big Sword"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["quantity"] == 10
        assert data["name"] == "Big Sword"
        assert data["type"] == "weapon"  # 未修改的字段保留

    def test_update_nonexistent_record(self) -> None:
        """更新不存在的记录应返回 404。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.put(
            "/api/v1/modules/test_mod/data/items/nonexistent-id",
            json={"name": "Test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


class TestCRUDDelete:
    """DELETE 删除记录测试。"""

    def test_delete_record_success(self) -> None:
        """成功删除记录。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        # 创建记录
        create_resp = client.post(
            "/api/v1/modules/test_mod/data/items",
            json={"name": "Sword", "type": "weapon"},
            headers={"Authorization": f"Bearer {token}"},
        )
        record_id = create_resp.json()["id"]

        # 删除记录
        resp = client.delete(
            f"/api/v1/modules/test_mod/data/items/{record_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True

        # 确认已删除
        list_resp = client.get(
            "/api/v1/modules/test_mod/data/items",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert list_resp.json()["total"] == 0

    def test_delete_nonexistent_record(self) -> None:
        """删除不存在的记录应返回 404。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import _clear_store  # noqa: PLC0415

        _clear_store()
        app = _create_app_with_crud()
        client = TestClient(app)
        token = _get_auth_token(client)

        resp = client.delete(
            "/api/v1/modules/test_mod/data/items/nonexistent-id",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


# ============================================================
# Access 模式控制测试
# ============================================================


class TestAccessControl:
    """access 模式控制测试。"""

    def test_read_only_no_write_routes(self) -> None:
        """read-only 模式应拒绝 POST/PUT/DELETE。"""
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store  # noqa: PLC0415

        _clear_store()
        generator = AutoCRUDGenerator()
        definition = {
            "fields": {
                "id": {"type": "uuid", "primary": True, "auto": True},
                "name": {"type": "string", "required": True},
            },
            "access": "read-only",
        }
        router = generator.register("ro_mod", "items", definition)
        assert router is not None

        from channels.api.app import create_app  # noqa: PLC0415

        app = create_app()
        app.include_router(router)
        client = TestClient(app)
        token = _get_auth_token(client)

        # GET 应该可用
        resp = client.get(
            "/api/v1/modules/ro_mod/data/items",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        # POST 应返回 405
        resp = client.post(
            "/api/v1/modules/ro_mod/data/items",
            json={"name": "Test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 405

        # PUT 应返回 405
        resp = client.put(
            "/api/v1/modules/ro_mod/data/items/some-id",
            json={"name": "Test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 405

        # DELETE 应返回 405
        resp = client.delete(
            "/api/v1/modules/ro_mod/data/items/some-id",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 405


# ============================================================
# 数据校验辅助函数测试
# ============================================================


class TestValidationHelpers:
    """数据校验辅助函数测试。"""

    def test_coerce_value_string(self) -> None:
        """字符串类型转换。"""
        from ui_schema.auto_crud import _coerce_value  # noqa: PLC0415

        assert _coerce_value("hello", "string") == "hello"
        assert _coerce_value(123, "string") == "123"

    def test_coerce_value_integer(self) -> None:
        """整数类型转换。"""
        from ui_schema.auto_crud import _coerce_value  # noqa: PLC0415

        assert _coerce_value("42", "integer") == 42
        assert _coerce_value(3.14, "integer") == 3

    def test_coerce_value_boolean(self) -> None:
        """布尔类型转换。"""
        from ui_schema.auto_crud import _coerce_value  # noqa: PLC0415

        assert _coerce_value("true", "boolean") is True
        assert _coerce_value("false", "boolean") is False
        assert _coerce_value(1, "boolean") is True
        assert _coerce_value(0, "boolean") is False

    def test_coerce_value_none(self) -> None:
        """None 值应保持 None。"""
        from ui_schema.auto_crud import _coerce_value  # noqa: PLC0415

        assert _coerce_value(None, "string") is None

    def test_validate_field_required(self) -> None:
        """必填字段校验。"""
        from ui_schema.auto_crud import _validate_field_value  # noqa: PLC0415

        error = _validate_field_value("name", None, {"required": True})
        assert error is not None
        assert "必填" in error

    def test_validate_field_enum(self) -> None:
        """枚举值校验。"""
        from ui_schema.auto_crud import _validate_field_value  # noqa: PLC0415

        error = _validate_field_value("type", "invalid", {"type": "enum", "values": ["a", "b"]})
        assert error is not None
        assert "不在允许范围" in error

    def test_validate_field_min_max(self) -> None:
        """数值范围校验。"""
        from ui_schema.auto_crud import _validate_field_value  # noqa: PLC0415

        error = _validate_field_value("qty", -1, {"type": "integer", "min": 0})
        assert error is not None
        assert "小于最小值" in error

    def test_validate_field_pass(self) -> None:
        """合法值应通过校验。"""
        from ui_schema.auto_crud import _validate_field_value  # noqa: PLC0415

        error = _validate_field_value("name", "hello", {"type": "string", "required": True})
        assert error is None


# ============================================================
# SchemaParser data 段解析测试
# ============================================================


class TestSchemaParserDataDecls:
    """SchemaParser data 段解析测试。"""

    def test_parse_data_section(self) -> None:
        """解析包含 data 段的 YAML。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            filepath = tmp_dir / "mod.yaml"
            filepath.write_text(
                yaml.dump(
                    {
                        "config_id": "data_mod",
                        "ui": {
                            "identity": {"id": "data_mod", "name": "Data Mod"},
                        },
                        "data": {
                            "items": {
                                "fields": {
                                    "id": {"type": "uuid", "primary": True, "auto": True},
                                    "name": {"type": "string", "required": True},
                                },
                                "access": "crud",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            parser = SchemaParser()
            parser.load_directory(tmp_dir)

            data_decls = parser.get_data_decls("data_mod")
            assert data_decls is not None
            assert "items" in data_decls
            assert data_decls["items"]["access"] == "crud"

    def test_parse_data_without_ui(self) -> None:
        """没有 ui 段但有 data 段的文件也能提取 data。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            filepath = tmp_dir / "data_only.yaml"
            filepath.write_text(
                yaml.dump(
                    {
                        "config_id": "data_only_mod",
                        "data": {
                            "settings": {
                                "fields": {
                                    "key": {"type": "string", "primary": True},
                                    "value": {"type": "string"},
                                },
                                "access": "read-only",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            parser = SchemaParser()
            parser.load_file(filepath)

            data_decls = parser.get_data_decls("data_only_mod")
            assert data_decls is not None
            assert "settings" in data_decls

    def test_list_all_data_decls(self) -> None:
        """list_all_data_decls 返回所有模块的 data 声明。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for i in range(2):
                filepath = tmp_dir / f"mod{i}.yaml"
                filepath.write_text(
                    yaml.dump(
                        {
                            "config_id": f"mod{i}",
                            "ui": {
                                "identity": {"id": f"mod{i}", "name": f"Mod{i}"},
                            },
                            "data": {
                                f"coll{i}": {
                                    "fields": {
                                        "id": {"type": "uuid", "primary": True},
                                    },
                                    "access": "crud",
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            parser = SchemaParser()
            parser.load_directory(tmp_dir)

            all_decls = parser.list_all_data_decls()
            assert len(all_decls) == 2
            assert "mod0" in all_decls
            assert "mod1" in all_decls

    def test_no_data_returns_none(self) -> None:
        """没有 data 段时 get_data_decls 返回 None。"""
        from ui_schema.parser import SchemaParser  # noqa: PLC0415

        parser = SchemaParser()
        assert parser.get_data_decls("nonexistent") is None
