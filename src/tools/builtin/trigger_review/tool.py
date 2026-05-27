"""
复盘触发工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TriggerReviewTool：复盘触发工具类
"""

import asyncio
import logging
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class TriggerReviewTool(BuiltinTool):
    """
    复盘触发工具

    提交复盘任务，分析最近的管道执行记录，产出经验和改进建议。
    支持强制触发（忽略触发条件检查）和防重复提交保护。
    """

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="trigger_review",
            description=(
                "提交复盘任务，分析最近的管道执行记录，"
                "产出经验和改进建议。完成后自动通知结果。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "force": {
                        "type": "boolean",
                        "description": "是否强制触发（忽略触发条件检查），默认 false",
                    },
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.AGENT,
            tags=["review", "maintenance", "system"],
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行复盘触发。

        通过 ServiceProvider 获取 maintenance_service，
        检查并发状态后异步提交复盘任务。

        Args:
            inputs: 输入参数，支持 force 字段强制触发

        Returns:
            ToolExecutionResult: 提交结果
        """
        force = inputs.get("force", False)

        # 获取 maintenance_service
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            maintenance_service = provider.get("maintenance_service")
            if maintenance_service is None:
                return create_failure_result(
                    error="维护服务不可用",
                    error_code="SERVICE_UNAVAILABLE",
                )
        except Exception as e:
            return create_failure_result(
                error=f"获取维护服务失败: {e}",
                error_code="SERVICE_UNAVAILABLE",
            )

        # 检查是否已在运行
        if maintenance_service._review_running:
            return create_success_result(
                data={"status": "already_running"},
                metadata={"message": "复盘正在执行中，请稍后再试"},
            )

        # 强制触发时跳过条件检查，直接执行复盘
        if not force and not maintenance_service.should_trigger_review():
            return create_success_result(
                data={"status": "skipped"},
                metadata={"message": "当前不满足触发条件，如需强制触发请设置 force=true"},
            )

        # 提交复盘任务（异步执行，不阻塞调用方）
        asyncio.create_task(
            maintenance_service.run_maintenance()
        )

        logger.info("[TriggerReview] 复盘任务已提交 (force=%s)", force)

        return create_success_result(
            data={"status": "submitted"},
            metadata={"message": "复盘任务已提交，完成后会通知您结果。"},
        )
