"""
WebSocket 统计信息 API 端点

提供 WebSocket 连接、性能和压缩统计信息的查询接口
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from src.api.websocket.compression import get_message_compressor
from src.api.websocket.handler import ConnectionManager
from src.api.websocket.monitoring import get_websocket_metrics
from src.auth.dependencies import get_current_user
from src.db.models import User

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(prefix="/api/websocket", tags=["WebSocket 统计"])

# 全局连接管理器实例（需要从主应用中注入）
_connection_manager: ConnectionManager | None = None


def set_connection_manager(manager: ConnectionManager) -> None:
    """设置连接管理器实例"""
    global _connection_manager
    _connection_manager = manager


def get_connection_manager() -> ConnectionManager:
    """获取连接管理器实例"""
    if _connection_manager is None:
        raise HTTPException(status_code=500, detail="连接管理器未初始化")
    return _connection_manager


@router.get("/stats", summary="获取 WebSocket 统计信息")
async def get_websocket_stats(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """
    获取 WebSocket 连接和性能统计信息

    返回：
    - 连接统计：总连接数、活跃连接数、线程数等
    - 性能统计：消息延迟、带宽使用、错误率等
    - 压缩统计：压缩率、节省带宽、压缩耗时等
    """
    try:
        # 获取连接管理器统计
        connection_manager = get_connection_manager()
        connection_stats = connection_manager.get_stats()

        # 获取性能监控统计
        metrics = get_websocket_metrics()
        performance_stats = metrics.export_metrics()

        # 获取压缩统计
        compressor = get_message_compressor()
        compression_stats = compressor.get_stats()

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "connections": connection_stats,
                    "performance": performance_stats,
                    "compression": compression_stats,
                    "timestamp": metrics._start_time.isoformat(),
                },
            }
        )

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取统计信息失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")


@router.get("/stats/connections", summary="获取连接统计")
async def get_connection_stats(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """
    获取 WebSocket 连接统计信息

    返回：
    - 总连接数、活跃连接数
    - 按线程和用户分组的连接数
    - 取消的线程数
    """
    try:
        connection_manager = get_connection_manager()
        stats = connection_manager.get_stats()

        return JSONResponse({"success": True, "data": stats})

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取连接统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取连接统计失败: {str(e)}")


@router.get("/stats/performance", summary="获取性能统计")
async def get_performance_stats(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """
    获取 WebSocket 性能统计信息

    返回：
    - 消息延迟统计（平均值、P99等）
    - 消息吞吐量统计
    - 错误率统计
    - 带宽使用统计
    """
    try:
        metrics = get_websocket_metrics()
        stats = metrics.export_metrics()

        return JSONResponse({"success": True, "data": stats})

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取性能统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取性能统计失败: {str(e)}")


@router.get("/stats/compression", summary="获取压缩统计")
async def get_compression_stats(
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """
    获取 WebSocket 消息压缩统计信息

    返回：
    - 压缩率统计
    - 节省带宽统计
    - 压缩耗时统计
    - 压缩消息比例
    """
    try:
        compressor = get_message_compressor()
        stats = compressor.get_stats()

        return JSONResponse({"success": True, "data": stats})

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取压缩统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取压缩统计失败: {str(e)}")


@router.get("/stats/threads", summary="获取线程统计")
async def get_thread_stats(
    limit: int = Query(10, ge=1, le=100, description="返回的线程数量限制"),
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """
    获取消息量最多的线程统计

    参数：
    - limit: 返回的线程数量限制（1-100）

    返回：
    - 按消息量排序的线程列表
    - 每个线程的连接数、消息数、字节数、错误数
    """
    try:
        metrics = get_websocket_metrics()
        top_threads = metrics.get_top_threads(limit)

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "threads": top_threads,
                    "total_threads": len(metrics._thread_stats),
                },
            }
        )

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取线程统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取线程统计失败: {str(e)}")


@router.get("/stats/users", summary="获取用户统计")
async def get_user_stats(
    limit: int = Query(10, ge=1, le=100, description="返回的用户数量限制"),
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """
    获取消息量最多的用户统计

    参数：
    - limit: 返回的用户数量限制（1-100）

    返回：
    - 按消息量排序的用户列表
    - 每个用户的连接数、消息数、字节数、错误数
    """
    try:
        metrics = get_websocket_metrics()
        top_users = metrics.get_top_users(limit)

        return JSONResponse(
            {
                "success": True,
                "data": {"users": top_users, "total_users": len(metrics._user_stats)},
            }
        )

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取用户统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取用户统计失败: {str(e)}")


@router.get("/stats/thread/{thread_id}", summary="获取指定线程统计")
async def get_thread_detail_stats(
    thread_id: str, current_user: User = Depends(get_current_user)
) -> JSONResponse:
    """
    获取指定线程的详细统计信息

    参数：
    - thread_id: 线程 ID

    返回：
    - 线程的连接数、消息数、字节数、错误数
    - 线程的活跃连接列表
    """
    try:
        metrics = get_websocket_metrics()
        connection_manager = get_connection_manager()

        # 获取线程统计
        thread_stats = metrics.get_thread_stats(thread_id)

        # 获取线程连接数
        connection_count = await connection_manager.get_thread_connection_count(
            thread_id
        )

        # 检查线程是否被取消
        is_cancelled = connection_manager.is_thread_cancelled(thread_id)

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "thread_id": thread_id,
                    "stats": thread_stats,
                    "connection_count": connection_count,
                    "is_cancelled": is_cancelled,
                },
            }
        )

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取线程 {thread_id} 统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取线程统计失败: {str(e)}")


@router.get("/stats/user/{user_id}", summary="获取指定用户统计")
async def get_user_detail_stats(
    user_id: str, current_user: User = Depends(get_current_user)
) -> JSONResponse:
    """
    获取指定用户的详细统计信息

    参数：
    - user_id: 用户 ID

    返回：
    - 用户的连接数、消息数、字节数、错误数
    - 用户的活跃连接数
    """
    try:
        metrics = get_websocket_metrics()
        connection_manager = get_connection_manager()

        # 获取用户统计
        user_stats = metrics.get_user_stats(user_id)

        # 获取用户连接数
        connection_count = await connection_manager.get_user_connection_count(user_id)

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "user_id": user_id,
                    "stats": user_stats,
                    "connection_count": connection_count,
                },
            }
        )

    except Exception as e:
        logger.error(f"[WebSocketStats] 获取用户 {user_id} 统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取用户统计失败: {str(e)}")


@router.post("/stats/reset", summary="重置统计信息")
async def reset_stats(current_user: User = Depends(get_current_user)) -> JSONResponse:
    """
    重置所有 WebSocket 统计信息

    注意：此操作将清除所有历史统计数据，请谨慎使用
    """
    try:
        # 重置性能监控统计
        metrics = get_websocket_metrics()
        metrics.reset_stats()

        # 重置压缩统计
        compressor = get_message_compressor()
        compressor.reset_stats()

        logger.info(f"[WebSocketStats] 用户 {current_user.id} 重置了统计信息")

        return JSONResponse({"success": True, "message": "统计信息已重置"})

    except Exception as e:
        logger.error(f"[WebSocketStats] 重置统计信息失败: {e}")
        raise HTTPException(status_code=500, detail=f"重置统计信息失败: {str(e)}")


@router.get("/health", summary="WebSocket 服务健康检查")
async def websocket_health_check() -> JSONResponse:
    """
    WebSocket 服务健康检查

    返回：
    - 服务状态
    - 基本统计信息
    - 系统资源使用情况
    """
    try:
        connection_manager = get_connection_manager()
        metrics = get_websocket_metrics()

        # 获取基本统计
        connection_count = await connection_manager.get_connection_count()
        global_stats = metrics.get_global_stats()

        # 判断服务健康状态
        is_healthy = True
        health_issues = []

        # 检查错误率
        if global_stats.get("error_count", 0) > 100:
            is_healthy = False
            health_issues.append("错误数量过多")

        # 检查平均延迟
        avg_latency = global_stats.get("avg_latency_ms", 0)
        if avg_latency > 1000:  # 1秒
            is_healthy = False
            health_issues.append("平均延迟过高")

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "healthy": is_healthy,
                    "issues": health_issues,
                    "connections": connection_count,
                    "uptime_seconds": global_stats.get("uptime_seconds", 0),
                    "avg_latency_ms": avg_latency,
                    "error_count": global_stats.get("error_count", 0),
                },
            }
        )

    except Exception as e:
        logger.error(f"[WebSocketStats] 健康检查失败: {e}")
        return JSONResponse(
            {
                "success": False,
                "data": {"healthy": False, "issues": [f"健康检查失败: {str(e)}"]},
            },
            status_code=500,
        )
