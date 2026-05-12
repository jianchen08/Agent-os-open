"""
健康检查 API 路由
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_async_session

router = APIRouter(tags=["health"])


@router.get("/check")
async def health_check(
    db: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """
    健康检查端点

    检查系统各组件的健康状态
    """
    try:
        # 检查数据库连接
        from sqlalchemy import text

        await db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    return {
        "status": "healthy" if db_status == "healthy" else "unhealthy",
        "timestamp": datetime.now().isoformat(),
        "components": {"database": db_status, "api": "healthy"},
        "version": "1.0.0",
    }


@router.get("/ping")
async def ping() -> dict[str, str]:
    """简单的ping端点"""
    return {"status": "ok", "message": "pong", "timestamp": datetime.now().isoformat()}
