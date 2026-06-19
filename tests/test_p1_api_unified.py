"""P1 架构修复 + API 统一 — 针对性测试。

覆盖以下 P1 变更：
- AR-3: message_bus.py 使用 engine.inject_queue_size 公共属性，不访问 _inject_queue
- AR-4: tools/builtin 中 send_pipeline_message 调用已迁移到 MessageBus.emit
- AR-5: 所有 API 路由前缀统一为 /api/v1/
- D-3: deps.py require_auth 仅保留 Authorization header（移除 Query token 参数）
- A-7: core/di/global_container.py 新增 get_service() 便捷封装
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ============================================================
# AR-5: API 路由前缀统一为 /api/v1/
# ============================================================


class TestAPIPrefixUnification:
    """验证所有路由模块的 prefix 均以 /api/v1/ 开头（AR-5）。"""

    # 从 app.py _register_routes 中提取的完整路由模块清单
    ROUTE_MODULES = [
        ("channels.api.routes_auth", "router"),
        ("channels.api.routes_threads", "router"),
        ("channels.api.routes_agents", "router"),
        ("channels.api.routes_tasks", "router"),
        ("channels.api.routes_tools", "router"),
        ("channels.api.routes_memory", "router"),
        ("channels.api.routes_evaluation", "router"),
        ("channels.api.routes_plugins", "router"),
        ("channels.api.routes_config", "router"),
        ("channels.api.routes_thinking_mode", "router"),
        ("channels.api.routes_ui", "router"),
        ("channels.api.routes_external_chat", "router"),
        ("channels.api.routes_scene", "router"),
        ("channels.api.routes_comfyui", "router"),
        ("channels.api.routes_maintenance", "router"),
    ]

    @pytest.mark.parametrize("module_path,router_attr", ROUTE_MODULES)
    def test_router_prefix_starts_with_api_v1(self, module_path: str, router_attr: str) -> None:
        """每个路由模块的 prefix 必须以 /api/v1/ 开头。"""
        import importlib

        mod = importlib.import_module(module_path)
        router = getattr(mod, router_attr)
        prefix = router.prefix

        assert prefix.startswith("/api/v1/"), (
            f"路由 {module_path}.{router_attr} 的 prefix='{prefix}' 不以 /api/v1/ 开头"
        )

    def test_workspaces_router_prefix(self) -> None:
        """工作空间路由 prefix 验证。"""
        from channels.api.routes_workspaces import workspaces_router

        assert workspaces_router.prefix.startswith("/api/v1/"), (
            f"workspaces_router prefix='{workspaces_router.prefix}' 不符合规范"
        )

    def test_reviews_router_prefix(self) -> None:
        """审批路由 prefix 验证。"""
        from channels.api.routes_reviews import reviews_router

        assert reviews_router.prefix.startswith("/api/v1/"), (
            f"reviews_router prefix='{reviews_router.prefix}' 不符合规范"
        )

    def test_artifacts_router_prefix(self) -> None:
        """制品路由 prefix 验证。"""
        from channels.api.routes_artifacts import artifacts_router

        assert artifacts_router.prefix.startswith("/api/v1/"), (
            f"artifacts_router prefix='{artifacts_router.prefix}' 不符合规范"
        )

    def test_no_legacy_api_prefix_without_v1(self) -> None:
        """确认不存在以 /api/ 开头但不是 /api/v1/ 的旧路由前缀。"""
        import importlib

        # 收集所有路由模块
        route_module_names = [m for m, _ in self.ROUTE_MODULES]
        route_module_names.extend([
            "channels.api.routes_workspaces",
            "channels.api.routes_reviews",
            "channels.api.routes_artifacts",
            "channels.api.routes_missing",
        ])

        for module_name in route_module_names:
            mod = importlib.import_module(module_name)
            # 查找模块中所有 APIRouter 实例
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if hasattr(attr, "prefix") and hasattr(attr, "routes") and attr_name.endswith("router"):
                    prefix = attr.prefix
                    if prefix.startswith("/api/"):
                        assert prefix.startswith("/api/v1"), (
                            f"{module_name}.{attr_name} 的 prefix='{prefix}' "
                            f"使用了旧版 /api/ 前缀而非 /api/v1/"
                        )


# ============================================================
# A-7: get_service() 便捷封装测试
# ============================================================


class TestGetService:
    """验证 core/di/global_container.py 的 get_service() 封装（A-7）。"""

    def test_get_service_returns_registered_instance(self) -> None:
        """注册服务后，get_service 应返回正确实例。"""
        from infrastructure.service_provider import ServiceProvider

        # 重置单例确保干净状态
        ServiceProvider.reset()
        provider = ServiceProvider()
        test_instance = {"name": "test_service_instance"}
        provider.register("test_service_a7", test_instance)

        from core.di.global_container import get_service

        result = get_service("test_service_a7")
        assert result is test_instance, "get_service 应返回已注册的实例"

        # 清理
        ServiceProvider.reset()

    def test_get_service_returns_default_for_missing(self) -> None:
        """服务不存在时，get_service 应返回 default 参数值。"""
        from core.di.global_container import get_service
        from infrastructure.service_provider import ServiceProvider

        ServiceProvider.reset()

        default_value = "fallback_default"
        result = get_service("nonexistent_service_xyz", default=default_value)
        assert result == default_value, "服务不存在时应返回 default 值"

        ServiceProvider.reset()

    def test_get_service_returns_none_for_missing_without_default(self) -> None:
        """无 default 参数且服务不存在时，应返回 None。"""
        from core.di.global_container import get_service
        from infrastructure.service_provider import ServiceProvider

        ServiceProvider.reset()

        result = get_service("totally_nonexistent_service")
        assert result is None, "无 default 参数时，缺失服务应返回 None"

        ServiceProvider.reset()

    def test_get_service_handles_provider_exception(self) -> None:
        """provider 内部异常时，get_service 应捕获并返回 default。

        get_service 设计上捕获 ImportError / AttributeError / KeyError，
        这些是运行时服务获取最可能遇到的异常类型。
        """
        from core.di.global_container import get_service

        # 模拟 get_service_provider 抛出 ImportError（如模块缺失场景）
        with patch(
            "infrastructure.service_provider.get_service_provider",
            side_effect=ImportError("provider module missing"),
        ):
            result = get_service("any_service", default="error_fallback")
            assert result == "error_fallback", "ImportError 时应返回 default 而非传播异常"

    def test_get_service_handles_attribute_error(self) -> None:
        """provider.get 不可用时（AttributeError），get_service 应返回 default。"""
        from core.di.global_container import get_service

        # 模拟 provider 对象缺少 get 方法
        with patch(
            "infrastructure.service_provider.get_service_provider",
            return_value=object(),  # 普通 object 没有 .get 方法
        ):
            result = get_service("any_service", default="attr_fallback")
            assert result == "attr_fallback", "AttributeError 时应返回 default"

    def test_get_service_signature_has_default_param(self) -> None:
        """get_service 函数签名必须包含 default 参数（A-7 封装核心）。"""
        from core.di.global_container import get_service

        sig = inspect.signature(get_service)
        params = sig.parameters

        assert "name" in params, "get_service 必须有 name 参数"
        assert "default" in params, "get_service 必须有 default 参数（A-7 便捷封装的核心设计）"
        assert params["default"].default is None, "default 参数的默认值应为 None"


# ============================================================
# D-3: require_auth header-only 认证测试
# ============================================================


class TestRequireAuthHeaderOnly:
    """验证 require_auth 仅接受 Authorization header，不接受 Query token（D-3）。"""

    def test_require_auth_accepts_valid_header(self) -> None:
        """正确的 Authorization Bearer header 应通过认证（2xx 场景）。"""
        from channels.api.auth import create_access_token
        from channels.api.deps import require_auth

        app = FastAPI()

        @app.get("/protected")
        async def _protected(user: dict = pytest.importorskip("fastapi").Depends(require_auth)):
            return {"user": user["username"]}

        token = create_access_token({"sub": "test_user_id", "username": "tester"})

        with TestClient(app) as client:
            resp = client.get(
                "/protected",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        assert resp.json()["user"] == "tester"

    def test_require_auth_rejects_missing_header(self) -> None:
        """无 Authorization header 应返回 401（4xx 场景）。"""
        from channels.api.deps import require_auth

        app = FastAPI()

        @app.get("/protected")
        async def _protected(user: dict = pytest.importorskip("fastapi").Depends(require_auth)):
            return {"user": user["username"]}

        with TestClient(app) as client:
            resp = client.get("/protected")

        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body or "error" in body, "401 响应应包含错误信息"

    def test_require_auth_rejects_invalid_token(self) -> None:
        """无效 token 应返回 401（4xx 场景）。"""
        from channels.api.deps import require_auth

        app = FastAPI()

        @app.get("/protected")
        async def _protected(user: dict = pytest.importorskip("fastapi").Depends(require_auth)):
            return {"user": user["username"]}

        with TestClient(app) as client:
            resp = client.get(
                "/protected",
                headers={"Authorization": "Bearer invalid_token_string"},
            )
        assert resp.status_code == 401

    def test_require_auth_rejects_empty_bearer(self) -> None:
        """空 Bearer token 应返回 401（边界场景）。"""
        from channels.api.deps import require_auth

        app = FastAPI()

        @app.get("/protected")
        async def _protected(user: dict = pytest.importorskip("fastapi").Depends(require_auth)):
            return {"user": user["username"]}

        with TestClient(app) as client:
            resp = client.get(
                "/protected",
                headers={"Authorization": "Bearer "},
            )
        assert resp.status_code == 401

    def test_require_auth_rejects_malformed_header(self) -> None:
        """非 Bearer 格式的 Authorization header 应返回 401。"""
        from channels.api.deps import require_auth

        app = FastAPI()

        @app.get("/protected")
        async def _protected(user: dict = pytest.importorskip("fastapi").Depends(require_auth)):
            return {"user": user["username"]}

        with TestClient(app) as client:
            resp = client.get(
                "/protected",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
        assert resp.status_code == 401

    def test_require_auth_no_query_token_param(self) -> None:
        """D-3 核心验证：require_auth 函数签名不应包含 Query token 参数。"""
        from channels.api.deps import require_auth

        sig = inspect.signature(require_auth)
        params = sig.parameters

        # 不应有 token 参数
        assert "token" not in params, (
            "D-3: require_auth 不应接受 Query token 参数，仅保留 Authorization header"
        )
        # 必须有 authorization 参数（Header 类型）
        assert "authorization" in params, (
            "require_auth 必须有 authorization header 参数"
        )

    def test_query_token_not_accepted(self) -> None:
        """D-3 验证：通过 ?token=xxx 查询参数传递 token 不应通过认证。"""
        from channels.api.auth import create_access_token
        from channels.api.deps import require_auth

        app = FastAPI()

        @app.get("/protected")
        async def _protected(user: dict = pytest.importorskip("fastapi").Depends(require_auth)):
            return {"user": user["username"]}

        valid_token = create_access_token({"sub": "test_user_id", "username": "tester"})

        with TestClient(app) as client:
            # 通过 query 参数传 token，不带 header
            resp = client.get(f"/protected?token={valid_token}")

        assert resp.status_code == 401, (
            "D-3: query token 参数不应被接受，只有 Authorization header 有效"
        )

    def test_extract_token_from_valid_header(self) -> None:
        """_extract_token 正确提取 Bearer token。"""
        from channels.api.deps import _extract_token

        token = _extract_token("Bearer my_token_123")
        assert token == "my_token_123"

    def test_extract_token_from_empty_header(self) -> None:
        """_extract_token 处理空 header 返回空字符串。"""
        from channels.api.deps import _extract_token

        assert _extract_token("") == ""
        assert _extract_token("Bearer ") == ""
        assert _extract_token("Basic something") == ""


# ============================================================
# AR-3: message_bus 使用 inject_queue_size 公共属性
# ============================================================


class TestMessageBusInjectQueueSize:
    """验证 message_bus 使用 engine.inject_queue_size 公共属性（AR-3）。"""

    def test_message_bus_source_uses_public_property(self) -> None:
        """AR-3: message_bus.py 源码中应使用 inject_queue_size 而非 _inject_queue。"""
        import os

        message_bus_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "pipeline", "message_bus.py"
        )
        message_bus_path = os.path.abspath(message_bus_path)

        with open(message_bus_path, encoding="utf-8") as f:
            source = f.read()

        # 不应直接访问 engine._inject_queue
        assert "engine._inject_queue" not in source, (
            "AR-3: message_bus.py 不应直接访问 engine._inject_queue 私有属性"
        )

    def test_engine_has_inject_queue_size_property(self) -> None:
        """engine.inject_queue_size 作为公共属性存在。"""
        # 通过源码验证属性定义存在
        import os

        engine_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "pipeline", "engine.py"
        )
        engine_path = os.path.abspath(engine_path)

        with open(engine_path, encoding="utf-8") as f:
            source = f.read()

        assert "def inject_queue_size" in source, (
            "AR-3: PipelineEngine 必须定义 inject_queue_size 公共属性"
        )
        assert "@property" in source, (
            "AR-3: inject_queue_size 必须是 @property"
        )

    def test_inject_queue_size_is_read_only_property(self) -> None:
        """inject_queue_size 是只读属性，内部委托给 _inject_queue。"""
        # 通过 AST 检查属性定义
        import ast
        import os

        engine_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "pipeline", "engine.py"
        )
        engine_path = os.path.abspath(engine_path)

        with open(engine_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        found_property = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                if node.name == "inject_queue_size":
                    # 检查是否有装饰器 @property
                    for decorator in node.decorator_list:
                        if isinstance(decorator, ast.Name) and decorator.id == "property":
                            found_property = True

        assert found_property, "inject_queue_size 必须使用 @property 装饰器"


# ============================================================
# AR-4: tools/builtin 中不再使用 send_pipeline_message
# ============================================================


class TestToolsBuiltinMessageBusMigration:
    """验证 tools/builtin 中 send_pipeline_message 已迁移到 MessageBus.emit（AR-4）。"""

    def test_no_send_pipeline_message_in_tools_builtin(self) -> None:
        """AR-4: tools/builtin 目录下不应再出现 send_pipeline_message 调用。"""
        import os

        tools_builtin_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "tools", "builtin"
        )
        tools_builtin_path = os.path.abspath(tools_builtin_path)

        if not os.path.exists(tools_builtin_path):
            pytest.skip("tools/builtin 目录不存在")

        violations = []
        for root, _dirs, files in os.walk(tools_builtin_path):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
                if "send_pipeline_message" in content:
                    # 检查是否是注释或字符串中的引用（粗略检查）
                    for lineno, line in enumerate(content.splitlines(), 1):
                        stripped = line.strip()
                        if (
                            "send_pipeline_message" in stripped
                            and not stripped.startswith("#")
                            and not stripped.startswith("'")
                            and not stripped.startswith('"')
                        ):
                            violations.append(f"{fpath}:{lineno}: {stripped}")

        assert len(violations) == 0, (
            f"AR-4: tools/builtin 中仍有 send_pipeline_message 残留:\n"
            + "\n".join(violations)
        )
