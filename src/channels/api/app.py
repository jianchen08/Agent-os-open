"""FastAPI 应用入口。

创建 FastAPI 应用实例，注册路由、中间件和错误处理器。
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from channels.api.deps import APIError, api_error_handler, generic_error_handler
from ui_schema.auth_types import AutoCRUDError
from channels.api.models import HealthResponse

logger = logging.getLogger(__name__)

_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5188",
    "http://localhost:5189",
    "http://localhost:5190",
]

# 应用启动时间
_start_time: float = 0.0


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    注册所有路由、添加 CORS 中间件、错误处理器和健康检查端点。

    Returns:
        配置好的 FastAPI 应用实例
    """
    global _start_time
    _start_time = time.time()

    app = FastAPI(
        title="Agent OS API",
        version="1.0.0",
        description=(
            "# Agent OS API\n\n"
            "Agent OS 后端 API 服务，提供以下功能：\n\n"
            "- **认证**: 登录、注册、令牌管理\n"
            "- **线程**: 会话线程 CRUD 与消息查询\n"
            "- **Agent 配置**: Agent 配置查询与筛选\n"
            "- **任务**: 任务 CRUD、提交、评估\n"
            "- **工具**: 工具注册查询\n"
            "- **记忆**: 记忆检索与管理\n"
            "- **评估指标**: 指标定义查询\n"
            "- **插件热重载**: 插件状态与重载\n\n"
            "## 认证方式\n\n"
            "所有受保护接口使用 Bearer Token 认证：\n\n"
            "```\n"
            "Authorization: Bearer <access_token>\n"
            "```\n\n"
            "或通过 query 参数传递：\n\n"
            "```\n"
            "?token=<access_token>\n"
            "```\n"
        ),
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # ---- 中间件 ----
    _add_middleware(app)

    # ---- 异常处理器 ----
    _add_exception_handlers(app)

    # ---- 注册路由 ----
    _register_routes(app)

    # ---- 健康检查 ----
    @app.get(
        "/health",
        tags=["健康检查"],
        summary="服务健康检查",
        response_model=HealthResponse,
    )
    def health_check() -> HealthResponse:
        """健康检查端点，返回服务状态、版本和运行时间。"""
        return HealthResponse(
            status="ok",
            version="1.0.0",
            uptime_seconds=round(time.time() - _start_time, 1),
        )

    # ---- 健康检查子路由 ----
    @app.get(
        "/health/live",
        tags=["健康检查"],
        summary="存活检查",
    )
    def liveness_check() -> dict[str, str]:
        return {"status": "alive"}

    @app.get(
        "/health/ready",
        tags=["健康检查"],
        summary="就绪检查",
    )
    def readiness_check() -> dict[str, str]:
        return {"status": "ready"}

    return app


def _add_middleware(app: FastAPI) -> None:
    """添加中间件：CORS + 限流 + 请求日志。"""
    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEFAULT_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 限流 + 请求日志中间件
    @app.middleware("http")
    async def rate_limit_and_log(
        request: Request,
        call_next,
    ) -> Response:
        """请求级中间件：IP 限流 + 请求日志。"""
        # 跳过健康检查和文档路径
        path = request.url.path
        if path in ("/health", "/api/docs", "/api/redoc", "/api/openapi.json"):
            return await call_next(request)

        # IP 限流
        client_ip = request.client.host if request.client else "unknown"
        from channels.api.deps import rate_limiter
        if not rate_limiter.is_allowed(client_ip):
            logger.warning("限流: IP %s 请求过于频繁", client_ip)
            return Response(
                content='{"error":{"code":"SYS_007","message":"请求过于频繁，请稍后重试"}}',
                status_code=429,
                media_type="application/json",
            )

        # 请求日志
        start = time.time()
        response = await call_next(request)
        duration_ms = round((time.time() - start) * 1000, 1)

        logger.info(
            "%s %s %s %d %.1fms",
            request.method,
            path,
            f"ip={client_ip}",
            response.status_code,
            duration_ms,
        )

        return response


def _add_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(AutoCRUDError, api_error_handler)
    app.add_exception_handler(Exception, generic_error_handler)


def _register_routes(app: FastAPI) -> None:
    """注册所有 API 路由。"""
    from channels.api.routes_agents import router as agents_router
    from channels.api.routes_auth import router as auth_router
    from channels.api.routes_config import router as config_router
    from channels.api.routes_evaluation import router as metrics_router
    from channels.api.routes_memory import router as memory_router
    from channels.api.routes_plugins import router as plugins_router
    from channels.api.routes_tasks import router as tasks_router
    from channels.api.routes_threads import router as threads_router
    from channels.api.routes_thinking_mode import (
        router as thinking_mode_router,
    )
    from channels.api.routes_tools import router as tools_router
    from channels.api.routes_ui import router as ui_router

    app.include_router(auth_router)
    app.include_router(threads_router)
    app.include_router(agents_router)
    app.include_router(tasks_router)
    app.include_router(tools_router)
    app.include_router(memory_router)
    app.include_router(metrics_router)
    app.include_router(plugins_router)
    app.include_router(config_router)
    app.include_router(thinking_mode_router)
    app.include_router(ui_router)

    # ---- 模块数据路由（手动注册的自定义端点） ----
    from channels.api.routes_ui import get_module_data_router

    app.include_router(get_module_data_router())

    # ---- 自动注册 Data CRUD 路由（基于 YAML data 声明） ----
    from channels.api.routes_ui import register_data_crud_routes

    for crud_router in register_data_crud_routes():
        app.include_router(crud_router)

    # ---- 补全缺失路由（前端期望但之前未注册） ----
    from channels.api.routes_missing import (
        projects_router,
        users_router,
        monitoring_router,
        triggers_router,
        interaction_router,
        agent_calls_router,
        execution_router,
        sessions_router,
        knowledge_base_router,
        floating_chat_router,
        cost_control_router,
        evaluation_router,
        eval_metrics_alias_router,
        client_router,
        files_router,
    )
    from channels.api.routes_artifacts import (
        artifacts_router,
        annotations_router_v1,
    )
    from channels.api.routes_reviews import reviews_router
    from channels.api.routes_workspaces import workspaces_router

    app.include_router(projects_router)
    app.include_router(users_router)
    app.include_router(monitoring_router)
    app.include_router(triggers_router)
    app.include_router(interaction_router)
    app.include_router(agent_calls_router)
    app.include_router(execution_router)
    app.include_router(sessions_router)
    app.include_router(knowledge_base_router)
    app.include_router(floating_chat_router)
    app.include_router(cost_control_router)
    app.include_router(evaluation_router)
    app.include_router(eval_metrics_alias_router)
    app.include_router(client_router)
    app.include_router(files_router)

    # ---- 审批与工作空间路由（新增） ----
    app.include_router(artifacts_router)
    app.include_router(annotations_router_v1)
    app.include_router(reviews_router)
    app.include_router(workspaces_router)
