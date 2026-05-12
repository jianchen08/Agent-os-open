"""
消息验证器

负责验证 WebSocket 消息格式
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """验证结果"""

    is_valid: bool
    content: str | None = None
    enable_thinking: bool = False
    error_code: str | None = None
    error_message: str | None = None


class MessageValidator:
    """
    消息验证器

    负责验证用户输入消息的格式和内容
    """

    def validate_user_input(self, data: dict[str, Any]) -> ValidationResult:
        """
        验证用户输入消息

        Args:
            data: 原始消息数据

        Returns:
            ValidationResult: 验证结果
        """
        logger.info(f"[MessageValidator] 开始验证用户输入 | data={data}")

        # 验证消息数据类型
        if not isinstance(data, dict):
            logger.error(f"[MessageValidator] 消息格式错误：期望字典类型，实际收到 {type(data).__name__}")
            return ValidationResult(
                is_valid=False,
                error_code="INVALID_MESSAGE_FORMAT",
                error_message=f'Invalid message format: expected object, received {type(data).__name__}',
            )

        # 验证消息格式 - 检查是否有 data 字段
        if "data" not in data or not isinstance(data.get("data"), dict):
            has_data = "data" in data
            data_type = type(data.get("data")).__name__ if has_data else "N/A"
            logger.error(f"[MessageValidator] 非标准格式消息 | data字段存在={has_data} | data类型={data_type}")
            return ValidationResult(
                is_valid=False,
                error_code="INVALID_MESSAGE_FORMAT",
                error_message='Invalid message format. Expected: {"type": "user_input", "data": {"content": "..."}}',
            )

        content = data.get("data", {}).get("content", "")
        enable_thinking = data.get("data", {}).get("enable_thinking", False)

        content_preview = content[:50] if content else "EMPTY"
        logger.info(f"[MessageValidator] 提取消息内容 | content={content_preview} | enable_thinking={enable_thinking}")

        # 验证内容不为空
        if not content:
            logger.error("[MessageValidator] 消息内容为空")
            return ValidationResult(
                is_valid=False,
                error_code="MISSING_CONTENT",
                error_message="Missing required field: content",
            )

        logger.info("[MessageValidator] 验证通过")
        return ValidationResult(
            is_valid=True,
            content=content,
            enable_thinking=enable_thinking,
        )

    def validate_regenerate_request(self, data: dict[str, Any]) -> ValidationResult:
        """
        验证重新生成请求

        Args:
            data: 原始消息数据

        Returns:
            ValidationResult: 验证结果
        """
        # 验证消息数据类型
        if not isinstance(data, dict):
            logger.error(f"[MessageValidator] 消息格式错误：期望字典类型，实际收到 {type(data).__name__}")
            return ValidationResult(
                is_valid=False,
                error_code="INVALID_MESSAGE_FORMAT",
                error_message=f'Invalid message format: expected object, received {type(data).__name__}',
            )

        if "data" not in data or not isinstance(data.get("data"), dict):
            return ValidationResult(
                is_valid=False,
                error_code="INVALID_MESSAGE_FORMAT",
                error_message="Invalid regenerate request format",
            )

        message_id = data.get("data", {}).get("message_id")
        if not message_id:
            return ValidationResult(
                is_valid=False,
                error_code="MISSING_MESSAGE_ID",
                error_message="Missing required field: message_id",
            )

        return ValidationResult(
            is_valid=True,
            content=message_id,
            enable_thinking=data.get("data", {}).get("enable_thinking", False),
        )
