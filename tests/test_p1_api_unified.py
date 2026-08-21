# @feature: FP-MIGR 0.1→0.2迁移清理 | @vision: V3 可嵌入 | @ci: none-local
"""P1 API 统一 — 存活路由前缀与鉴权回归（0.2 清理版，批次 2 §4.1）。

0.2 清理（2026-08-19，逐用例分类清单见当次 commit message）：

删除（A 类，0.1 快照断言）：
- routes_auth / routes_agents / routes_tools / routes_plugins / routes_maintenance
  前缀参数用例——模块已删，对应 API 面迁 kernel/crates/api（axum）。
- routes_ui / routes_comfyui / routes_reviews 前缀用例——文件残留但 import 链断
  （ui_schema / services / review.* sidecar 未迁移），server.py 已按域 stub 防御
  （server.py modules/ui 与 reviews 域的 try/except 分发），断言死模块前缀属
  0.1 快照。
- TestGetService ×6——core/di/global_container + infrastructure.service_provider
  （0.1 Python DI）已删，0.2 DI 落 Rust kernel。
- TestMessageBusInjectQueueSize ×3 与 TestToolsBuiltinMessageBusMigration——断言对象
  src/pipeline/{message_bus,engine}.py、src/tools/builtin/ 已随 src/ 删除
  （消息总线/引擎在 kernel engine crate）。

改写（B 类）：
- test_no_legacy_api_prefix_without_v1——改为扫描 0.2 存活路由模块
  （channel_api sidecar 按域挂载的全部 routes_*），意图不变：
  不允许 /api/（无 v1）旧前缀回潮。
- 模块级 use_channel("api")——注册 channels.api 命名空间兼容（tests/channels/
  conftest.py _register_channels_api_compat），本文件可独立运行
  （原先依赖 test_delete_thread_cascade_metadata.py 先行导入才不炸）。

保留（0.2 仍有效意图）：
- AR-5: channel_api 存活路由模块 prefix 均以 /api/v1/ 开头。
- D-3: channels.api.deps.require_auth 仅接受 Authorization header，
  不接受 Query token。
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.channels.conftest import use_channel

use_channel("api")

# ============================================================
# AR-5: API 路由前缀统一为 /api/v1/（0.2 存活路由模块）
# ============================================================


class TestAPIPrefixUnification:
    """验证 0.2 存活路由模块的 prefix 均以 /api/v1/ 开头（AR-5）。"""

    # 0.2 channel_api sidecar 实际按域分发（server.py import routes_xxx）且
    # import 链存活的模块清单（routes_threads/routes_external_chat 已于批次 0
    # 删除——死代码，未挂 http.handle 分派）。
    ROUTE_MODULES = [
        ("channels.api.routes_tasks", "router"),
        ("channels.api.routes_memory", "router"),
        ("channels.api.routes_evaluation", "router"),
        ("channels.api.routes_config", "router"),
        ("channels.api.routes_thinking_mode", "router"),
        ("channels.api.routes_scene", "router"),
    ]

    @pytest.mark.parametrize("module_path,router_attr", ROUTE_MODULES)
    def test_router_prefix_starts_with_api_v1(self, module_path: str, router_attr: str) -> None:
        """每个存活路由模块的 prefix 必须以 /api/v1/ 开头。"""
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

    def test_artifacts_router_prefix(self) -> None:
        """制品路由 prefix 验证。"""
        from channels.api.routes_artifacts import artifacts_router

        assert artifacts_router.prefix.startswith("/api/v1/"), (
            f"artifacts_router prefix='{artifacts_router.prefix}' 不符合规范"
        )

    def test_no_legacy_api_prefix_without_v1(self) -> None:
        """确认存活路由模块中不存在以 /api/ 开头但不是 /api/v1/ 的旧路由前缀。"""
        import importlib

        # 扫描范围 = 参数化存活模块 + workspaces/artifacts/search/missing
        # （均为 server.py 按域分发且 import 链存活的模块）。
        route_module_names = [m for m, _ in self.ROUTE_MODULES]
        route_module_names.extend([
            "channels.api.routes_workspaces",
            "channels.api.routes_artifacts",
            "channels.api.routes_search",
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
