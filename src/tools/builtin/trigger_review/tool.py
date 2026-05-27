"""复盘触发工具。

暴露接口：
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
    """复盘触发工具。

    通过管道消息注入触发复盘，与定时触发使用相同的执行路径。
    Agent 可在对话中调用此工具，用户说"帮我复盘一下"即可触发。
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
            injected_params=["pipeline_id"],
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行复盘触发。

        通过管道消息注入机制提交复盘任务。
        如果管道不可用则降级直接执行。

        Args:
            inputs: 输入参数，含 force 和注入的 pipeline_id

        Returns:
            ToolExecutionResult: 提交结果
        """
        force = inputs.get("force", False)
        pipeline_id = inputs.get("pipeline_id", "")

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

        if getattr(maintenance_service, "_review_running", False):
            return create_success_result(
                data={"status": "already_running"},
                metadata={"message": "复盘正在执行中，请稍后再试"},
            )

        if not force and not maintenance_service.should_trigger_review():
            pending = maintenance_service._get_review_engine()._count_pending_records()
            return create_success_result(
                data={"status": "skipped", "pending_records": pending},
                metadata={"message": f"当前不满足触发条件（待复盘记录 {pending} 条），如需强制触发请设置 force=true"},
            )

        # 通过管道消息注入触发复盘
        if pipeline_id:
            try:
                from pipeline.message_bus import send_pipeline_message
                result = await send_pipeline_message(
                    pipeline_id,
                    "[复盘触发] 请执行复盘任务，分析最近的管道执行记录，产出经验和改进建议。",
                    metadata={"source": "manual_review", "force": force},
                )
                if result.success:
                    logger.info(
                        "[TriggerReview] 复盘任务已通过管道提交 (pipeline=%s, force=%s)",
                        pipeline_id, force,
                    )
                    return create_success_result(
                        data={"status": "submitted", "pipeline_id": pipeline_id},
                        metadata={"message": "复盘任务已提交，完成后会通知您结果。"},
                    )
                logger.warning("[TriggerReview] 管道消息注入失败: %s", result.error)
            except Exception as exc:
                logger.warning("[TriggerReview] 管道消息注入异常: %s", exc)

        # 降级：管道不可用时直接异步执行
        async def _run_review_direct() -> None:
            """降级直接执行复盘。"""
            try:
                await maintenance_service.trigger_review(force=force)
                logger.info("[TriggerReview] 复盘任务已完成（直接执行）")
            except Exception as exc:
                logger.error("[TriggerReview] 复盘任务执行失败: %s", exc)

        asyncio.create_task(_run_review_direct())

        logger.info("[TriggerReview] 复盘任务已提交（直接执行, force=%s)", force)

        return create_success_result(
            data={"status": "submitted"},
            metadata={"message": "复盘任务已提交，完成后会通知您结果。"},
        )
