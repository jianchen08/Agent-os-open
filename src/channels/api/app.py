"""FastAPI 应用入口。

创建 FastAPI 应用实例，注册路由和中间件。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from channels.api.routes_auth import router as auth_router
from channels.api.routes_threads import router as threads_router


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    注册所有路由、添加 CORS 中间件和健康检查端点。

    Returns:
        配置好的 FastAPI 应用实例
    """
    app = FastAPI(
        title="Agent OS API",
        version="1.0.0",
        description="Agent OS 后端 API 服务",
    )

    # CORS 中间件配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5188",
            "http://localhost:5189",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(auth_router)
    app.include_router(threads_router)

    @app.get("/health", tags=["健康检查"], summary="服务健康检查")
    def health_check() -> dict[str, str]:
        """健康检查端点，返回服务状态。"""
        return {"status": "ok"}

    return app
