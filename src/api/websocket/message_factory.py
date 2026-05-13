"""
简化的消息工厂

统一消息创建接口，简化版本管理
"""

import uuid
from datetime import datetime
from typing import Any

from .message_types import MessageTypes


class MessageFactory:
    """
    简化的消息工厂

    提供统一的消息创建接口，自动处理版本管理
    """

    @staticmethod
    def create_message(
        message_type: str,
        thread_id: str,
        data: dict[str, Any],
        message_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """
        创建标准消息

        Args:
            message_type: 消息类型
            thread_id: 线程ID
            data: 消息数据
            message_id: 消息ID（可选，自动生成）
            timestamp: 时间戳（可选，自动生成）

        Returns:
            标准格式的消息字典
        """
        # BUG-FIX: 在 data 中自动注入 pipeline_id，默认回退到 thread_id
        # 修复原因：前端 resolvePipelineId 依赖 data.pipeline_id 路由消息到正确的管道
        enriched_data = {**data, "pipeline_id": data.get("pipeline_id", thread_id)}
        return {
            "type": message_type,
            "message_id": message_id or str(uuid.uuid4()),
            "thread_id": thread_id,
            "timestamp": timestamp or datetime.utcnow().isoformat(),
            "data": enriched_data,
        }

    @staticmethod
    def create_user_input(
        thread_id: str,
        content: str,
        attachments: list | None = None,
    ) -> dict[str, Any]:
        """创建用户输入消息"""
        data = {"content": content}
        if attachments:
            data["attachments"] = attachments

        return MessageFactory.create_message(
            MessageTypes.USER_INPUT,
            thread_id,
            data,
        )

    @staticmethod
    def create_new_message(
        thread_id: str,
        message_id: str,
        role: str,
        content: str,
        version_info: dict[str, Any] | None = None,
        has_error: bool = False,
        error_detail: str | None = None,
        thinking: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> dict[str, Any]:
        """
        创建新消息通知

        Args:
            thread_id: 线程ID
            message_id: 消息ID
            role: 角色 (user/assistant)
            content: 消息内容
            version_info: 版本信息（可选）
            has_error: 是否有错误
            error_detail: 错误详情
            thinking: 思考内容（可选）
            tool_calls: 工具调用列表（可选）

        Returns:
            new_message消息
        """
        data = {
            "message_id": message_id,
            "thread_id": thread_id,
            "role": role,
            "content": content,
        }

        if version_info:
            data["version_info"] = version_info
        if has_error:
            data["has_error"] = has_error
        if error_detail:
            data["error_detail"] = error_detail
        if thinking:
            data["thinking"] = thinking
        if tool_calls:
            data["tool_calls"] = tool_calls

        return MessageFactory.create_message(
            MessageTypes.NEW_MESSAGE,
            thread_id,
            data,
            message_id=message_id,
        )

    @staticmethod
    def create_stream_message(
        thread_id: str,
        ai_message_id: str,
        chunk: str | None = None,
        is_start: bool = False,
        is_end: bool = False,
        final_message_id: str | None = None,
        cancelled: bool = False,
        parent_message_id: str | None = None,
        is_retry: bool = False,
    ) -> dict[str, Any]:
        """
        创建流式消息（统一处理开始、片段、结束）

        Args:
            thread_id: 线程ID
            ai_message_id: AI消息ID
            chunk: 消息片段（片段消息时使用）
            is_start: 是否为开始消息
            is_end: 是否为结束消息
            final_message_id: 最终消息ID（结束消息时使用）
            cancelled: 是否被取消（结束消息时使用）
            parent_message_id: 父消息ID（重新生成时使用，指向原消息）
            is_retry: 是否为重试（重新生成）

        Returns:
            流式消息
        """
        if is_start:
            data = {"ai_message_id": ai_message_id}
            if parent_message_id:
                data["parent_message_id"] = parent_message_id
            if is_retry:
                data["is_retry"] = is_retry
            # BUG-FIX: 传递 message_id=ai_message_id，避免生成随机 UUID
            # 导致前端创建重复的消息占位符
            return MessageFactory.create_message(
                MessageTypes.STREAM_START,
                thread_id,
                data,
                message_id=ai_message_id,
            )
        elif is_end:
            data = {
                "ai_message_id": ai_message_id,
                "final_message_id": final_message_id or ai_message_id,
            }
            if cancelled:
                data["cancelled"] = cancelled
            # BUG-FIX: 传递 message_id=ai_message_id，避免生成随机 UUID
            return MessageFactory.create_message(
                MessageTypes.STREAM_END,
                thread_id,
                data,
                message_id=ai_message_id,
            )
        else:
            # BUG-FIX: 片段消息同样传递 message_id=ai_message_id，保持一致性
            return MessageFactory.create_message(
                MessageTypes.STREAM_CHUNK,
                thread_id,
                {"chunk": chunk or "", "ai_message_id": ai_message_id},
                message_id=ai_message_id,
            )

    @staticmethod
    def create_tool_message(
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
        ai_message_id: str,
        parameters: dict[str, Any] | None = None,
        status: str | None = None,
        result: Any | None = None,
        error: str | None = None,
        progress: float | None = None,
        current_step: str | None = None,
        is_start: bool = False,
        is_end: bool = False,
        is_progress: bool = False,
    ) -> dict[str, Any]:
        """
        创建工具相关消息（统一处理开始、进度、结束）

        Args:
            thread_id: 线程ID
            tool_call_id: 工具调用ID
            tool_name: 工具名称
            ai_message_id: AI消息ID
            parameters: 工具参数
            status: 状态
            result: 结果
            error: 错误信息
            progress: 进度
            current_step: 当前步骤
            is_start: 是否为开始消息
            is_end: 是否为结束消息
            is_progress: 是否为进度消息

        Returns:
            工具消息
        """
        base_data = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "ai_message_id": ai_message_id,
        }

        if is_start:
            base_data.update(
                {
                    "parameters": parameters or {},
                }
            )
            return MessageFactory.create_message(
                MessageTypes.TOOL_CALL_START,
                thread_id,
                base_data,
            )
        elif is_end:
            base_data.update(
                {
                    "status": status or "completed",
                }
            )
            if result is not None:
                base_data["result"] = result
            if error:
                base_data["error"] = error
            return MessageFactory.create_message(
                MessageTypes.TOOL_CALL_END,
                thread_id,
                base_data,
            )
        elif is_progress:
            base_data.update(
                {
                    "progress": progress or 0.0,
                }
            )
            if current_step:
                base_data["current_step"] = current_step
            return MessageFactory.create_message(
                MessageTypes.TOOL_CALL_PROGRESS,
                thread_id,
                base_data,
            )

    @staticmethod
    def create_error_message(
        thread_id: str,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建错误消息"""
        data = {
            "error_code": error_code,
            "message": message,
        }
        if details:
            data["details"] = details

        return MessageFactory.create_message(
            MessageTypes.ERROR,
            thread_id,
            data,
        )

    @staticmethod
    def create_system_message(
        thread_id: str,
        message_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """创建系统消息（心跳、连接确认等）"""
        return MessageFactory.create_message(
            message_type,
            thread_id,
            data,
        )

    @staticmethod
    def create_thinking_message(
        thread_id: str,
        ai_message_id: str,
        content: str | None = None,
        is_start: bool = False,
        is_end: bool = False,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """
        创建思考过程消息

        Args:
            thread_id: 线程ID
            ai_message_id: AI消息ID
            content: 思考内容（片段消息时使用）
            is_start: 是否为开始消息
            is_end: 是否为结束消息
            duration_ms: 思考耗时（结束消息时使用）

        Returns:
            思考消息
        """
        if is_start:
            return MessageFactory.create_message(
                MessageTypes.THINKING_START,
                thread_id,
                {"ai_message_id": ai_message_id},
            )
        elif is_end:
            data = {"ai_message_id": ai_message_id}
            if duration_ms is not None:
                data["duration_ms"] = duration_ms
            return MessageFactory.create_message(
                MessageTypes.THINKING_END,
                thread_id,
                data,
            )
        else:
            # 思考内容片段
            return MessageFactory.create_message(
                MessageTypes.THINKING_CHUNK,
                thread_id,
                {"chunk": content or "", "ai_message_id": ai_message_id},
            )
