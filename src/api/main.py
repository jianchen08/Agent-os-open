"""
FastAPI 应用入口

提供 API 应用的创建和配置
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.error_handler import setup_exception_handlers
from src.api.lifespan import StartupManager
from src.api.middleware import setup_middlewares
from src.api.rate_limit import RateLimitConfig
from src.api.routes import v1_router
from src.api.websocket.endpoint_handler import WebSocketEndpointHandler
from src.db.connection import get_async_session

logger = logging.getLogger(__name__)


def create_app(
    title: str = "元思考 Agent 系统 API",
    description: str = "提供 Agent 管理、工作流执行、记忆检索等功能的 API 服务",
    version: str = "1.0.0",
    cors_origins: list[str] | None = None,
    rate_limit_config: RateLimitConfig | None = None,
    debug: bool = False,
) -> FastAPI:
    """创建 FastAPI 应用实例"""

    # 第一件事：配置日志（确保在任何模块导入之前）
    from src.config.logging import setup_logging

    setup_logging()

    # 创建启动管理器
    startup_manager = StartupManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期管理"""
        # 启动逻辑
        await startup_manager.startup()

        # yield 控制权给应用运行
        yield

        # 关闭逻辑
        await startup_manager.shutdown()

    app = FastAPI(
        lifespan=lifespan,
        title=title,
        description=description,
        version=version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        debug=debug,
        openapi_tags=[
            {"name": "认证", "description": "用户认证和授权相关接口"},
            {"name": "Agent", "description": "Agent 管理接口"},
            {"name": "工具", "description": "工具查询和管理接口"},
            {"name": "工作流", "description": "工作流管理接口"},
            {"name": "执行控制", "description": "任务执行控制接口"},
            {"name": "记忆", "description": "记忆检索和管理接口"},
            {"name": "系统", "description": "系统状态和配置接口"},
        ],
    )

    # 配置中间件
    setup_middlewares(
        app, cors_origins=cors_origins, rate_limit_config=rate_limit_config
    )

    # 注册全局异常处理器
    setup_exception_handlers(app)

    # 注册路由
    app.include_router(v1_router)

    # 注册 WebSocket 端点
    @app.websocket("/ws/chat/{thread_id}")
    async def websocket_endpoint(
        websocket: WebSocket,
        thread_id: str,
        db: AsyncSession = Depends(get_async_session),
        token: str | None = None,  # JWT Token（可选）
    ):
        """WebSocket 聊天端点 - 使用端点处理器管理连接生命周期

        支持 JWT 认证（可选）:
        - 开发模式: 允许匿名访问（user_id = "anonymous"）
        - 生产模式: 需要提供有效的 JWT Token

        Token 传递方式:
        - 查询参数: ?token=xxx 或 ?access_token=xxx
        """
        from src.api.websocket.auth import get_user_id_from_websocket

        # 验证 Token 并获取用户 ID
        user_id = await get_user_id_from_websocket(websocket, token, db)

        # 创建端点处理器并处理连接
        handler = WebSocketEndpointHandler(websocket, thread_id, db, user_id)
        await handler.handle()

    # 健康检查端点
    @app.get("/health", tags=["系统"])
    async def health_check():
        """健康检查"""
        return {"status": "healthy"}

    # 就绪检查端点
    @app.get("/ready", tags=["系统"])
    async def ready_check():
        """就绪检查"""
        return {"status": "ready"}

    # 版本信息端点
    @app.get("/version", tags=["系统"])
    async def get_version():
        """获取版本信息"""
        return {"version": version, "api_version": "v1"}

    return app


# 默认应用实例（从配置读取 debug 参数）
from src.config.settings import get_settings

_settings = get_settings()
app = create_app(debug=_settings.debug)
