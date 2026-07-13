"""FastAPI 应用入口。

创建 FastAPI 应用实例，注册路由、中间件和错误处理器。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from channels.api.deps import APIError, api_error_handler, generic_error_handler
from channels.api.models import HealthResponse
from src.core.logging import LogContext
from ui_schema.auth_types import AutoCRUDError

logger = logging.getLogger(__name__)

# CORS 允许源通过环境变量 CORS_ORIGINS 管理（逗号分隔）。
# 默认值覆盖本地开发 + Docker host 两种模式：
#   - localhost:5188/5289/5290/5173 → 本地或端口转发访问
#   - host.docker.internal:8988    → 前端容器内 server.py 代理 WebSocket 时，
#      websockets 库按目标 URL 派生的 Origin（Starlette>=0.38 对 WS 做 Origin 检查）
_DEFAULT_CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:5188,http://localhost:5289,http://localhost:5290,http://localhost:5173,http://host.docker.internal:8988",
).split(",")

# 应用启动时间
_start_time: float = 0.0


def create_app(lifespan: Any = None) -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    注册所有路由、添加 CORS 中间件、错误处理器和健康检查端点。

    Args:
        lifespan: 可选的 ASGI lifespan 上下文管理器

    Returns:
        配置好的 FastAPI 应用实例
    """
    global _start_time  # noqa: PLW0603
    _start_time = time.time()

    app = FastAPI(
        lifespan=lifespan,
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
        """请求级中间件：IP 限流 + 请求日志 + 链路追踪上下文绑定。"""
        # 跳过健康检查和文档路径
        path = request.url.path
        if path in ("/health", "/api/docs", "/api/redoc", "/api/openapi.json"):
            return await call_next(request)

        # 为每个请求生成 request_id，绑定链路追踪上下文
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        trace_id = request.headers.get("X-Trace-ID", request_id)

        with LogContext.scoped(request_id=request_id, trace_id=trace_id):
            # IP 差异化限流（按请求类别：读/写/删除/认证/上传）
            client_ip = request.client.host if request.client else "unknown"
            from channels.api.rate_limiter import tiered_rate_limiter  # noqa: PLC0415

            if not tiered_rate_limiter.is_request_allowed(client_ip, request.method, path):
                logger.warning("限流: IP %s 请求 %s %s 过于频繁", client_ip, request.method, path)
                return Response(
                    content='{"error":{"code":"SYS_LOAD_8002","message":"请求过于频繁，请稍后重试"}}',
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


def _register_routes(app: FastAPI) -> None:  # noqa: PLR0915
    """注册所有 API 路由。"""
    from channels.api.routes_agents import router as agents_router  # noqa: PLC0415
    from channels.api.routes_auth import router as auth_router  # noqa: PLC0415
    from channels.api.routes_config import router as config_router  # noqa: PLC0415
    from channels.api.routes_evaluation import router as metrics_router  # noqa: PLC0415
    from channels.api.routes_external_chat import router as external_chat_router  # noqa: PLC0415
    from channels.api.routes_memory import router as memory_router  # noqa: PLC0415
    from channels.api.routes_plugins import router as plugins_router  # noqa: PLC0415
    from channels.api.routes_tasks import router as tasks_router  # noqa: PLC0415
    from channels.api.routes_themes import router as themes_router  # noqa: PLC0415
    from channels.api.routes_thinking_mode import (  # noqa: PLC0415
        router as thinking_mode_router,
    )
    from channels.api.routes_threads import router as threads_router  # noqa: PLC0415
    from channels.api.routes_tools import router as tools_router  # noqa: PLC0415
    from channels.api.routes_ui import router as ui_router  # noqa: PLC0415

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
    app.include_router(themes_router)
    app.include_router(ui_router)

    # ---- 外部系统路由 ----
    app.include_router(external_chat_router)

    # ---- 模块数据路由（手动注册的自定义端点） ----
    from channels.api.routes_ui import get_module_data_router  # noqa: PLC0415

    app.include_router(get_module_data_router())

    # ---- 自动注册 Data CRUD 路由（基于 YAML data 声明） ----
    from channels.api.routes_ui import register_data_crud_routes  # noqa: PLC0415

    for crud_router in register_data_crud_routes():
        app.include_router(crud_router)

    # ---- 补全缺失路由（前端期望但之前未注册） ----
    from channels.api.routes_artifacts import (  # noqa: PLC0415
        annotations_router_v1,
        artifacts_router,
    )
    from channels.api.routes_asr import asr_router  # noqa: PLC0415
    from channels.api.routes_missing import (  # noqa: PLC0415
        agent_calls_router,
        client_router,
        cost_control_router,
        eval_metrics_alias_router,
        evaluation_router,
        execution_router,
        files_router,
        floating_chat_router,
        interaction_router,
        knowledge_base_router,
        monitoring_router,
        projects_router,
        sessions_router,
        task_phase_router,
        triggers_router,
        users_router,
    )
    from channels.api.routes_reviews import reviews_router  # noqa: PLC0415
    from channels.api.routes_workspaces import workspaces_router  # noqa: PLC0415

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
    app.include_router(task_phase_router)

    # ---- 审批与工作空间路由（新增） ----
    app.include_router(artifacts_router)
    app.include_router(annotations_router_v1)
    app.include_router(reviews_router)
    app.include_router(workspaces_router)

    # ---- 语音识别（ASR）路由 ----
    app.include_router(asr_router)

    # ---- ComfyUI 路由（新增） ----
    from channels.api.routes_comfyui import router as comfyui_router  # noqa: PLC0415

    app.include_router(comfyui_router)

    # ---- 场景管理路由 ----
    from channels.api.routes_scene import router as scene_router  # noqa: PLC0415

    app.include_router(scene_router)

    # ---- 维护管理路由 ----
    from channels.api.routes_maintenance import router as maintenance_router  # noqa: PLC0415

    app.include_router(maintenance_router)
