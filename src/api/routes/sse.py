"""
SSE (Server-Sent Events) 路由模块

提供实时事件推送功能，包括系统通知和日志流
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.api.dependencies import get_current_user
from src.api.services.notification_service import NotificationService
from src.core.constants import QueryLimits
from src.db.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["SSE"])


@router.get("/notifications")
async def notification_stream(
    request: Request, current_user: User = Depends(get_current_user)
) -> StreamingResponse:
    """
    系统通知 SSE 流

    为当前用户提供实时系统通知推送
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        """通知事件生成器"""
        notification_service = NotificationService()

        try:
            logger.info(f"[SSE] 用户 {current_user.id} 开始订阅通知流")

            while True:
                # 检查客户端是否断开连接
                if await request.is_disconnected():
                    logger.info(f"[SSE] 用户 {current_user.id} 断开通知流连接")
                    break

                try:
                    # 获取用户的未读通知
                    notifications = await notification_service.get_unread_notifications(
                        user_id=current_user.id,
                        limit=QueryLimits.SSE_NOTIFICATION_LIMIT,
                    )

                    # 推送新通知
                    for notification in notifications:
                        data = {
                            "id": str(notification.id),
                            "type": notification.type,
                            "title": notification.title,
                            "message": notification.message,
                            "timestamp": notification.created_at.isoformat(),
                            "priority": notification.priority,
                            "read": notification.read,
                        }

                        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

                        # 标记为已推送
                        await notification_service.mark_as_pushed(notification.id)

                    # 每5秒检查一次新通知
                    await asyncio.sleep(5)

                except Exception as e:
                    logger.error(f"[SSE] 通知流处理错误: {e}")
                    # 发送错误事件
                    error_data = {
                        "type": "error",
                        "message": "通知服务暂时不可用",
                        "timestamp": datetime.now().isoformat(),
                    }
                    yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(10)  # 错误时延长等待时间

        except asyncio.CancelledError:
            logger.info(f"[SSE] 用户 {current_user.id} 通知流被取消")
        except Exception as e:
            logger.error(f"[SSE] 通知流异常: {e}")
        finally:
            logger.info(f"[SSE] 用户 {current_user.id} 通知流结束")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Cache-Control",
        },
    )


@router.get("/logs")
async def log_stream(
    request: Request,
    level: str = Query("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """
    实时日志 SSE 流

    仅管理员可访问，提供实时日志查看功能
    """
    # 权限检查
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限才能访问日志流")

    async def log_event_generator() -> AsyncGenerator[str, None]:
        """日志事件生成器"""
        log_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        log_handler = SSELogHandler(log_queue, level)

        # 添加到根日志记录器
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            logger.info(
                f"[SSE] 管理员 {current_user.id} 开始订阅日志流 (级别: {level})"
            )

            while True:
                # 检查客户端是否断开连接
                if await request.is_disconnected():
                    logger.info(f"[SSE] 管理员 {current_user.id} 断开日志流连接")
                    break

                try:
                    # 等待新日志，超时1秒
                    log_record = await asyncio.wait_for(log_queue.get(), timeout=1.0)

                    data = {
                        "timestamp": log_record["timestamp"],
                        "level": log_record["level"],
                        "logger": log_record["logger"],
                        "message": log_record["message"],
                        "module": log_record.get("module"),
                        "line": log_record.get("line"),
                        "thread": log_record.get("thread"),
                    }

                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

                except TimeoutError:
                    # 发送心跳保持连接
                    heartbeat_data = {
                        "type": "heartbeat",
                        "timestamp": datetime.now().isoformat(),
                    }
                    yield f"data: {json.dumps(heartbeat_data, ensure_ascii=False)}\n\n"

        except asyncio.CancelledError:
            logger.info(f"[SSE] 管理员 {current_user.id} 日志流被取消")
        except Exception as e:
            logger.error(f"[SSE] 日志流异常: {e}")
        finally:
            # 清理日志处理器
            root_logger.removeHandler(log_handler)
            logger.info(f"[SSE] 管理员 {current_user.id} 日志流结束")

    return StreamingResponse(
        log_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class SSELogHandler(logging.Handler):
    """SSE 日志处理器"""

    def __init__(self, queue: asyncio.Queue, level: str):
        super().__init__()
        self.queue = queue
        self.setLevel(getattr(logging, level.upper()))

        # 设置日志格式
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        """发送日志记录到队列"""
        try:
            log_data = {
                "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "module": getattr(record, "module", None),
                "line": getattr(record, "lineno", None),
                "thread": getattr(record, "thread", None),
            }

            # 非阻塞放入队列
            try:
                self.queue.put_nowait(log_data)
            except asyncio.QueueFull:
                # 队列满时丢弃最旧的日志
                try:
                    self.queue.get_nowait()
                    self.queue.put_nowait(log_data)
                except asyncio.QueueEmpty:
                    pass

        except Exception:
            # 避免日志处理器本身的错误影响应用
            self.handleError(record)


@router.get("/health")
async def sse_health_check() -> dict:
    """SSE 服务健康检查"""
    return {
        "status": "healthy",
        "service": "SSE Events",
        "timestamp": datetime.now().isoformat(),
        "endpoints": ["/events/notifications", "/events/logs"],
    }
