"""Round 3 — REST API 端点存在性与覆盖测试。

验证需求文档 §2.2 列出的所有 REST 路由模块在代码中正确注册路由，
以及 auto_crud 安全约束和控制命令参数的完整性。

对应需求：F-UI-27, F-UI-28, F-UI-29, AC-UI-10
来源：
  - docs/requirements/各模块需求文档/04_前端交互模块需求文档.md（§2.2 REST API 路由）
  - .project/api_contract.md

覆盖内容：
1. 14 个 REST 路由模块的注册与前缀验证（通过 import + 检查 router.routes）
2. auto_crud 端点 × 安全约束（read_only / read_write / read_create / crud）
3. 控制命令参数验证（stop_generation / resume_action 的参数完整性）
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ui_schema.auto_crud import AutoCRUDGenerator, _clear_store


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _fake_auth() -> Any:
    """模拟认证依赖，返回固定用户信息。"""
    async def _dep() -> dict[str, str]:
        return {"user_id": "test-user", "username": "tester", "role": "admin"}
    return _dep


def _register_crud_exception_handler(app: FastAPI) -> None:
    """注册 AutoCRUDError 异常处理器，使其返回正确的 HTTP 状态码。"""
    from fastapi import Request  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from ui_schema.auth_types import AutoCRUDError  # noqa: PLC0415

    @app.exception_handler(AutoCRUDError)
    async def _handle_crud_error(request: Request, exc: AutoCRUDError):  # type: ignore[no-untyped-def]
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


def _make_app_with_crud(
    module_id: str,
    collection: str,
    definition: dict[str, Any],
) -> tuple[FastAPI, TestClient]:
    """创建带单个 CRUD 路由的 FastAPI 应用（auth 已绕过）。"""
    _clear_store()
    app = FastAPI()

    generator = AutoCRUDGenerator()
    router = generator.register(module_id, collection, definition)
    assert router is not None, f"CRUD 路由注册失败: {module_id}/{collection}"
    app.include_router(router)

    # 注册 AutoCRUDError 异常处理器
    _register_crud_exception_handler(app)

    # 绕过 auth
    from channels.api.deps import require_auth  # noqa: PLC0415
    app.dependency_overrides[require_auth] = _fake_auth()

    client = TestClient(app)
    return app, client


def _count_methods(router: APIRouter, method: str) -> int:
    """统计路由器中指定 HTTP 方法的路由数。"""
    return sum(
        1 for r in router.routes
        if hasattr(r, "methods") and method in r.methods
    )


def _has_method(router: APIRouter, method: str) -> bool:
    """检查路由器是否包含指定 HTTP 方法的路由。"""
    return _count_methods(router, method) > 0


def _base_fields() -> dict[str, dict[str, Any]]:
    """基础字段定义。"""
    return {
        "id": {"type": "uuid", "primary": True, "auto": True},
        "name": {"type": "string", "required": True},
        "quantity": {"type": "integer", "default": 1, "min": 0},
    }


# ===========================================================================
# 一、REST 路由模块注册验证
# ===========================================================================

class TestRouteModuleRegistration:
    """验证需求 §2.2 列出的所有 REST 路由模块在代码中注册了路由。

    每个路由模块必须：
    1. 可正常 import
    2. 暴露 router 对象（带正确 prefix）
    3. 至少注册了 1 条路由

    需求来源：04_前端交互模块需求文档.md §2.2
    """

    # 需求文档 §2.2 中列出的路由模块及期望前缀
    EXPECTED_ROUTES: list[tuple[str, str, str]] = [
        # (模块文件名, router 变量名, 期望前缀)
        ("routes_config", "router", "/api/v1/config"),
        ("routes_threads", "router", "/api/v1/threads"),
        ("routes_tasks", "router", "/api/v1/tasks"),
        ("routes_agents", "router", "/api/v1/agents"),
        ("routes_tools", "router", "/api/v1/tools"),
        ("routes_evaluation", "router", "/api/v1/metrics"),
        ("routes_workspaces", "workspaces_router", "/api/v1/workspaces"),
        ("routes_memory", "router", "/api/v1/memory"),
        ("routes_auth", "router", "/api/v1/auth"),
        ("routes_plugins", "router", "/api/v1/plugins"),
        ("routes_ui", "router", "/api/v1/modules/ui"),
        ("routes_scene", "router", "/api/v1/scenes"),
        ("routes_maintenance", "router", "/api/v1/maintenance"),
        ("routes_thinking_mode", "router", "/api/v1/thinking-mode"),
    ]

    def test_all_route_modules_importable(self) -> None:
        """所有 14 个路由模块均可成功 import 并暴露 router 对象。"""
        from channels.api import (  # noqa: PLC0415
            routes_agents,
            routes_auth,
            routes_config,
            routes_evaluation,
            routes_maintenance,
            routes_memory,
            routes_plugins,
            routes_scene,
            routes_tasks,
            routes_thinking_mode,
            routes_threads,
            routes_tools,
            routes_ui,
            routes_workspaces,
        )

        modules = [
            routes_config, routes_threads, routes_tasks, routes_agents,
            routes_tools, routes_evaluation, routes_workspaces, routes_memory,
            routes_auth, routes_plugins, routes_ui, routes_scene,
            routes_maintenance, routes_thinking_mode,
        ]
        for mod in modules:
            assert mod is not None, f"路由模块 {mod.__name__} 导入失败"

    @pytest.mark.parametrize(
        "module_name, router_attr, expected_prefix",
        EXPECTED_ROUTES,
    )
    def test_route_prefix_matches_contract(
        self,
        module_name: str,
        router_attr: str,
        expected_prefix: str,
    ) -> None:
        """每个路由模块的 prefix 必须与 API 契约一致。"""
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(f"channels.api.{module_name}")
        router = getattr(mod, router_attr)
        assert isinstance(router, APIRouter), (
            f"{module_name}.{router_attr} 不是 APIRouter 实例"
        )
        assert router.prefix == expected_prefix, (
            f"{module_name} prefix={router.prefix!r}, 期望={expected_prefix!r}"
        )

    @pytest.mark.parametrize(
        "module_name, router_attr, expected_prefix",
        EXPECTED_ROUTES,
    )
    def test_route_has_endpoints(
        self,
        module_name: str,
        router_attr: str,
        expected_prefix: str,
    ) -> None:
        """每个路由模块至少注册了 1 条路由端点。"""
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(f"channels.api.{module_name}")
        router = getattr(mod, router_attr)
        assert len(router.routes) > 0, (
            f"{module_name} 未注册任何路由端点"
        )

    def test_auto_crud_prefix_pattern(self) -> None:
        """auto_crud 路由前缀模式为 /api/v1/modules/{module}/data/{collection}。"""
        _clear_store()
        gen = AutoCRUDGenerator()
        router = gen.register("demo_mod", "demo_col", {
            "fields": _base_fields(),
            "access": "crud",
        })
        assert router is not None
        assert router.prefix == "/api/v1/modules/demo_mod/data/demo_col"


# ===========================================================================
# 二、auto_crud 端点 × 安全约束（access 控制）
# ===========================================================================

class TestAutoCRUDAccessControl:
    """验证 F-UI-29 安全约束：access 控制四种模式。

    需求 F-UI-29: 安全约束：access 控制（read_only / read_write / read_create / crud）

    access 模式与可用操作映射：
    | 模式          | GET(列表+单条) | POST(创建) | PUT(更新) | DELETE(删除) |
    |---------------|:---:|:---:|:---:|:---:|
    | read_only     |  ✓  |     |     |     |
    | read_write    |  ✓  |     |  ✓  |     |
    | read_create   |  ✓  |  ✓  |     |     |
    | crud          |  ✓  |  ✓  |  ✓  |  ✓  |
    """

    def test_read_only_has_get_only(self) -> None:
        """read_only 模式：仅注册 GET 路由，不注册 POST/PUT/DELETE。"""
        _clear_store()
        gen = AutoCRUDGenerator()
        router = gen.register("acc_test", "read_only_col", {
            "fields": _base_fields(),
            "access": "read_only",
        })
        assert router is not None
        assert _has_method(router, "GET"), "read_only 必须有 GET 路由"
        assert not _has_method(router, "POST"), "read_only 不应有 POST"
        assert not _has_method(router, "PUT"), "read_only 不应有 PUT"
        assert not _has_method(router, "DELETE"), "read_only 不应有 DELETE"

    def test_read_write_has_get_and_put(self) -> None:
        """read_write 模式：注册 GET + PUT，不注册 POST/DELETE。"""
        _clear_store()
        gen = AutoCRUDGenerator()
        router = gen.register("acc_test", "read_write_col", {
            "fields": _base_fields(),
            "access": "read_write",
        })
        assert router is not None
        assert _has_method(router, "GET"), "read_write 必须有 GET 路由"
        assert _has_method(router, "PUT"), "read_write 必须有 PUT 路由"
        assert not _has_method(router, "POST"), "read_write 不应有 POST"
        assert not _has_method(router, "DELETE"), "read_write 不应有 DELETE"

    def test_read_create_has_get_and_post(self) -> None:
        """read_create 模式：注册 GET + POST，不注册 PUT/DELETE。"""
        _clear_store()
        gen = AutoCRUDGenerator()
        router = gen.register("acc_test", "read_create_col", {
            "fields": _base_fields(),
            "access": "read_create",
        })
        assert router is not None
        assert _has_method(router, "GET"), "read_create 必须有 GET 路由"
        assert _has_method(router, "POST"), "read_create 必须有 POST 路由"
        assert not _has_method(router, "PUT"), "read_create 不应有 PUT"
        assert not _has_method(router, "DELETE"), "read_create 不应有 DELETE"

    def test_crud_has_all_methods(self) -> None:
        """crud 模式：注册 GET + POST + PUT + DELETE 全部路由。"""
        _clear_store()
        gen = AutoCRUDGenerator()
        router = gen.register("acc_test", "crud_col", {
            "fields": _base_fields(),
            "access": "crud",
        })
        assert router is not None
        assert _has_method(router, "GET"), "crud 必须有 GET 路由"
        assert _has_method(router, "POST"), "crud 必须有 POST 路由"
        assert _has_method(router, "PUT"), "crud 必须有 PUT 路由"
        assert _has_method(router, "DELETE"), "crud 必须有 DELETE 路由"

    def test_read_only_rejects_write_requests(self) -> None:
        """read_only 模式下 POST/PUT/DELETE 请求应返回 405。"""
        _, client = _make_app_with_crud("ro_mod", "items", {
            "fields": _base_fields(),
            "access": "read_only",
        })

        base = "/api/v1/modules/ro_mod/data/items"

        # GET 应成功
        resp = client.get(base)
        assert resp.status_code == 200

        # POST 应 405
        assert client.post(base, json={"name": "X"}).status_code == 405
        # PUT 应 405
        assert client.put(f"{base}/fake-id", json={"name": "X"}).status_code == 405
        # DELETE 应 405
        assert client.delete(f"{base}/fake-id").status_code == 405

    def test_read_write_rejects_create_and_delete(self) -> None:
        """read_write 模式下 POST 和 DELETE 请求应返回 405。"""
        _, client = _make_app_with_crud("rw_mod", "items", {
            "fields": _base_fields(),
            "access": "read_write",
        })

        base = "/api/v1/modules/rw_mod/data/items"

        # GET 应成功
        assert client.get(base).status_code == 200
        # PUT 应成功（即使记录不存在返回 404，不是 405）
        put_resp = client.put(f"{base}/fake-id", json={"name": "X"})
        assert put_resp.status_code != 405, "read_write 不应拒绝 PUT"
        # POST 应 405
        assert client.post(base, json={"name": "X"}).status_code == 405
        # DELETE 应 405
        assert client.delete(f"{base}/fake-id").status_code == 405

    def test_read_create_rejects_update_and_delete(self) -> None:
        """read_create 模式下 PUT 和 DELETE 请求应返回 405。"""
        _, client = _make_app_with_crud("rc_mod", "items", {
            "fields": _base_fields(),
            "access": "read_create",
        })

        base = "/api/v1/modules/rc_mod/data/items"

        # GET 应成功
        assert client.get(base).status_code == 200
        # POST 应成功
        post_resp = client.post(base, json={"name": "NewItem"})
        assert post_resp.status_code == 200
        # PUT 应 405
        assert client.put(f"{base}/fake-id", json={"name": "X"}).status_code == 405
        # DELETE 应 405
        assert client.delete(f"{base}/fake-id").status_code == 405

    def test_backward_compat_hyphenated_access(self) -> None:
        """向后兼容：连字符格式（read-only / write-only）应正常工作。"""
        _clear_store()
        gen = AutoCRUDGenerator()

        # read-only → 等价于 read_only
        router_ro = gen.register("compat", "ro", {
            "fields": _base_fields(),
            "access": "read-only",
        })
        assert router_ro is not None
        assert _has_method(router_ro, "GET")
        assert not _has_method(router_ro, "POST")

        # write-only → 等价于 write_only（仅 POST）
        router_wo = gen.register("compat", "wo", {
            "fields": _base_fields(),
            "access": "write-only",
        })
        assert router_wo is not None
        assert _has_method(router_wo, "POST")


# ===========================================================================
# 三、控制命令参数验证
# ===========================================================================

class TestControlCommandParams:
    """验证控制命令 stop_generation / resume_action 的参数完整性。

    需求来源：04_前端交互模块需求文档.md §2.1 控制命令
    - stop_generation: 停止生成（设置 state[SHOULD_STOP]=True）
    - resume_action + approved:true → 审批通过
    - resume_action + approved:false → 审批拒绝

    验证点：parse_frontend_message 正确解析控制命令的参数。
    """

    def test_stop_generation_parsed_as_control(self) -> None:
        """stop_generation 消息应被解析为 MessageType.CONTROL。"""
        from pipeline.message_handler import parse_frontend_message  # noqa: PLC0415
        from pipeline.message_types import MessageType  # noqa: PLC0415

        raw = {
            "type": "stop_generation",
            "data": {
                "thread_id": "thread-123",
                "pipeline_id": "pipe-456",
            },
        }
        msg = parse_frontend_message(raw)
        assert msg.type == MessageType.CONTROL
        assert msg.thread_id == "thread-123"
        assert msg.pipeline_id == "pipe-456"

    def test_stop_generation_with_envelope_format(self) -> None:
        """stop_generation 消息（平铺格式）应正确解析 pipeline_id。"""
        from pipeline.message_handler import parse_frontend_message  # noqa: PLC0415
        from pipeline.message_types import MessageType  # noqa: PLC0415

        raw = {
            "type": "stop_generation",
            "thread_id": "thread-789",
            "pipeline_id": "pipe-abc",
        }
        msg = parse_frontend_message(raw)
        assert msg.type == MessageType.CONTROL
        assert msg.pipeline_id == "pipe-abc"
        assert msg.thread_id == "thread-789"

    def test_resume_action_parsed_as_control(self) -> None:
        """resume_action 消息应被解析为 MessageType.CONTROL。"""
        from pipeline.message_handler import parse_frontend_message  # noqa: PLC0415
        from pipeline.message_types import MessageType  # noqa: PLC0415

        raw = {
            "type": "resume_action",
            "data": {
                "thread_id": "thread-001",
                "pipeline_id": "pipe-002",
                "approved": True,
            },
        }
        msg = parse_frontend_message(raw)
        assert msg.type == MessageType.CONTROL
        assert msg.thread_id == "thread-001"
        assert msg.pipeline_id == "pipe-002"

    def test_resume_action_approved_true_preserved(self) -> None:
        """resume_action + approved:true 的 metadata 应保留 approved 字段。"""
        from pipeline.message_handler import parse_frontend_message  # noqa: PLC0415

        raw = {
            "type": "resume_action",
            "data": {
                "thread_id": "t1",
                "pipeline_id": "p1",
                "approved": True,
                "request_id": "req-001",
            },
        }
        msg = parse_frontend_message(raw)
        # CONTROL 类型消息，metadata 包含完整 data
        assert msg.metadata.get("approved") is True
        assert msg.metadata.get("request_id") == "req-001"

    def test_resume_action_approved_false_preserved(self) -> None:
        """resume_action + approved:false 的 metadata 应保留 approved 字段。"""
        from pipeline.message_handler import parse_frontend_message  # noqa: PLC0415

        raw = {
            "type": "resume_action",
            "data": {
                "thread_id": "t2",
                "pipeline_id": "p2",
                "approved": False,
                "request_id": "req-002",
            },
        }
        msg = parse_frontend_message(raw)
        assert msg.metadata.get("approved") is False

    def test_control_message_metadata_contains_full_data(self) -> None:
        """CONTROL 类型消息的 metadata 应包含完整 data 字典。

        验证 parse_frontend_message 对 CONTROL 类型消息的特殊处理：
        metadata=dict(data)，保留所有原始字段。
        """
        from pipeline.message_handler import parse_frontend_message  # noqa: PLC0415

        raw = {
            "type": "stop_generation",
            "data": {
                "thread_id": "t3",
                "pipeline_id": "p3",
                "reason": "user_cancelled",
                "extra_param": "value",
            },
        }
        msg = parse_frontend_message(raw)
        # CONTROL 消息 metadata 应包含原始 data 的全部内容
        assert "reason" in msg.metadata
        assert msg.metadata["reason"] == "user_cancelled"
        assert msg.metadata["extra_param"] == "value"

    def test_missing_type_raises_error(self) -> None:
        """缺少 type 字段的消息应抛出 MessageParseError。"""
        from pipeline.message_handler import (  # noqa: PLC0415
            MessageParseError,
            parse_frontend_message,
        )

        with pytest.raises(MessageParseError):
            parse_frontend_message({"data": {"thread_id": "t1"}})

    def test_non_dict_message_raises_error(self) -> None:
        """非字典类型的消息应抛出 MessageParseError。"""
        from pipeline.message_handler import (  # noqa: PLC0415
            MessageParseError,
            parse_frontend_message,
        )

        with pytest.raises(MessageParseError):
            parse_frontend_message("not a dict")  # type: ignore[arg-type]

    def test_stop_generation_missing_pipeline_id_still_parses(self) -> None:
        """stop_generation 缺少 pipeline_id 时仍应正确解析（空字符串）。

        参数完整性验证：解析层不校验业务约束（pipeline_id 是否存在），
        只负责结构化解析。业务校验由 app_factory 的处理逻辑负责。
        """
        from pipeline.message_handler import parse_frontend_message  # noqa: PLC0415
        from pipeline.message_types import MessageType  # noqa: PLC0415

        raw = {
            "type": "stop_generation",
            "data": {"thread_id": "t4"},
        }
        msg = parse_frontend_message(raw)
        assert msg.type == MessageType.CONTROL
        assert msg.pipeline_id == ""  # 缺失时为空字符串
