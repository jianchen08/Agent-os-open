"""
控制命令处理器

处理停止生成、恢复操作、心跳、执行控制等控制命令
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.websocket.handlers.base import BaseHandler, HandlerContext
from src.api.websocket.message_bus import SourceType, get_message_bus
from src.api.websocket.message_factory import MessageFactory
from src.api.websocket.message_types import MessageTypes
from src.db.models import ExecutionRecord

logger = logging.getLogger(__name__)


async def _update_execution_record_status(
    db: AsyncSession,
    execution_id: str,
    new_status: str,
    action: str,
) -> bool:
    """
    更新执行记录的状态

    Args:
        db: 数据库会话
        execution_id: 执行记录ID
        new_status: 新状态 (paused/running/cancelled)
        action: 操作类型 (pause/resume/cancel/stop)

    Returns:
        bool: 是否更新成功
    """
    try:
        result = await db.execute(
            select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
        )
        record = result.scalar_one_or_none()

        if not record:
            logger.warning(
                f"[_update_execution_record_status] 执行记录不存在 | execution_id={execution_id}"
            )
            return False

        # 更新 message_data 中的状态
        message_data = record.message_data.copy()
        old_status = message_data.get("status", "unknown")
        message_data["status"] = new_status

        # 新结构中不再记录中间状态的时间戳，只更新状态
        record.message_data = message_data
        await db.commit()

        logger.info(
            f"[_update_execution_record_status] 状态已更新 | "
            f"execution_id={execution_id} | {old_status} -> {new_status}"
        )
        return True

    except Exception as e:
        logger.error(
            f"[_update_execution_record_status] 更新失败 | execution_id={execution_id} | error={e}",
            exc_info=True,
        )
        await db.rollback()
        return False


class ControlHandler(BaseHandler):
    """控制命令处理器"""

    # 支持的消息类型
    SUPPORTED_TYPES = {
        "stop_generation",
        "resume_action",
        "heartbeat",
        "execution_control",
    }

    def can_handle(self, message_type: str) -> bool:
        return message_type in self.SUPPORTED_TYPES

    async def handle(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """处理控制命令"""
        message_type = data.get("type")

        if message_type == "stop_generation":
            return await self._handle_stop(ctx, data)
        elif message_type == "resume_action":
            return await self._handle_resume(ctx, data)
        elif message_type == "heartbeat":
            return await self._handle_heartbeat(ctx, data)
        elif message_type == "execution_control":
            return await self._handle_execution_control(ctx, data)

        return None

    async def _handle_stop(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """处理停止生成请求"""
        logger.info(f"收到停止生成请求 | thread_id={ctx.thread_id}")

        # 更新当前正在执行的消息状态为 cancelled
        # 查找当前正在执行的消息
        result = await ctx.db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == ctx.thread_id,
                ExecutionRecord.message_data["status"].as_string() == "running",
            )
            .order_by(ExecutionRecord.created_at.desc())
            .limit(1)
        )
        current_record = result.scalar_one_or_none()

        if current_record:
            await _update_execution_record_status(
                ctx.db, current_record.id, "cancelled", "stop"
            )

        # 发送停止确认响应
        response = MessageFactory.create_message(
            message_type=MessageTypes.EXECUTION_CONTROL_RESPONSE,
            thread_id=ctx.thread_id,
            data={
                "action": "cancel",
                "success": True,
                "message": "已停止生成",
                "new_status": "cancelled",
            },
        )
        await get_message_bus().emit(
            ctx.thread_id, response, source_type=SourceType.SYSTEM, source_id="control"
        )
        return None

    async def _handle_resume(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """处理恢复操作请求"""
        action = data.get("action")
        logger.info(f"收到恢复操作 | thread_id={ctx.thread_id} | action={action}")

        # 查找当前暂停的消息
        result = await ctx.db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == ctx.thread_id,
                ExecutionRecord.message_data["status"].as_string() == "paused",
            )
            .order_by(ExecutionRecord.created_at.desc())
            .limit(1)
        )
        paused_record = result.scalar_one_or_none()

        if paused_record:
            await _update_execution_record_status(
                ctx.db, paused_record.id, "running", "resume"
            )

        # 发送恢复确认响应
        response = MessageFactory.create_message(
            message_type=MessageTypes.EXECUTION_CONTROL_RESPONSE,
            thread_id=ctx.thread_id,
            data={
                "action": "resume",
                "success": True,
                "message": "已恢复执行",
                "new_status": "running",
            },
        )
        await get_message_bus().emit(
            ctx.thread_id, response, source_type=SourceType.SYSTEM, source_id="control"
        )
        return None

    async def _handle_heartbeat(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """处理心跳消息"""
        logger.debug(f"收到心跳消息 | thread_id={ctx.thread_id}")

        heartbeat_ack = MessageFactory.create_message(
            message_type=MessageTypes.HEARTBEAT_ACK,
            thread_id=ctx.thread_id,
            data={"client_timestamp": data.get("timestamp")},
        )
        await get_message_bus().emit(
            ctx.thread_id,
            heartbeat_ack,
            source_type=SourceType.SYSTEM,
            source_id="heartbeat",
        )
        return None

    async def _handle_execution_control(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        处理执行控制命令（暂停/恢复/取消）

        消息格式：
        {
            "type": "execution_control",
            "data": {
                "execution_id": "xxx",
                "action": "pause" | "resume" | "cancel",
                "reason": "可选原因"
            }
        }
        """
        msg_data = data.get("data", {})
        execution_id = msg_data.get("execution_id")
        action = msg_data.get("action")
        reason = msg_data.get("reason", "")

        logger.info(
            f"收到执行控制命令 | thread_id={ctx.thread_id} | "
            f"execution_id={execution_id} | action={action} | reason={reason}"
        )

        # 验证参数
        if not execution_id or not action:
            response = MessageFactory.create_message(
                message_type=MessageTypes.EXECUTION_CONTROL_RESPONSE,
                thread_id=ctx.thread_id,
                data={
                    "execution_id": execution_id,
                    "action": action,
                    "success": False,
                    "message": "缺少必要参数：execution_id 或 action",
                },
            )
            await get_message_bus().emit(
                ctx.thread_id,
                response,
                source_type=SourceType.SYSTEM,
                source_id="control",
            )
            return None

        if action not in ("pause", "resume", "cancel"):
            response = MessageFactory.create_message(
                message_type=MessageTypes.EXECUTION_CONTROL_RESPONSE,
                thread_id=ctx.thread_id,
                data={
                    "execution_id": execution_id,
                    "action": action,
                    "success": False,
                    "message": f"不支持的操作：{action}",
                },
            )
            await get_message_bus().emit(
                ctx.thread_id,
                response,
                source_type=SourceType.SYSTEM,
                source_id="control",
            )
            return None

        # 根据 action 执行相应操作
        new_status = None
        message = ""

        if action == "pause":
            # 更新执行记录状态为 paused
            success = await _update_execution_record_status(
                ctx.db, execution_id, "paused", "pause"
            )
            new_status = "paused"
            message = "执行已暂停" if success else "暂停失败（执行记录不存在）"
            logger.info(f"执行已暂停 | execution_id={execution_id} | success={success}")

        elif action == "resume":
            # 更新执行记录状态为 running
            success = await _update_execution_record_status(
                ctx.db, execution_id, "running", "resume"
            )
            new_status = "running"
            message = "执行已恢复" if success else "恢复失败（执行记录不存在）"
            logger.info(f"执行已恢复 | execution_id={execution_id} | success={success}")

        elif action == "cancel":
            # 更新执行记录状态为 cancelled
            success = await _update_execution_record_status(
                ctx.db, execution_id, "cancelled", "cancel"
            )
            new_status = "cancelled"
            message = "执行已取消" if success else "取消失败（执行记录不存在）"
            logger.info(f"执行已取消 | execution_id={execution_id} | success={success}")

        # 发送响应
        response = MessageFactory.create_message(
            message_type=MessageTypes.EXECUTION_CONTROL_RESPONSE,
            thread_id=ctx.thread_id,
            data={
                "execution_id": execution_id,
                "action": action,
                "success": True,
                "message": message,
                "new_status": new_status,
            },
        )
        await get_message_bus().emit(
            ctx.thread_id, response, source_type=SourceType.SYSTEM, source_id="control"
        )
        return None
