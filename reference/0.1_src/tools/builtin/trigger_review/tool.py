"""复盘触发工具。

仅作为触发接口。所有复盘编排逻辑（注册管道、等待完成、持久化、
通知回写）统一由 MemoryMaintenanceService 管理，见：
  memory/maintenance/service.py 中的 trigger_llm_review 及相关方法。
"""

import logging
from typing import Any

from pipeline.engine_state import _current_pipeline_id
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

    调用 maintenance_service.trigger_llm_review() 启动全链路复盘编排。
    本身不包含任何业务逻辑，只做参数透传和结果返回。

    run_on_main_loop=True：必须在主管道事件循环中执行。若走独立线程的
    临时事件循环，asyncio.create_task 创建的后台 task 会被 loop.close()
    级联杀掉，导致复盘管道静默失败。
    """

    run_on_main_loop: bool = True

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="trigger_review",
            description=(
                "提交一次异步复盘：启动 review_agent 对最近的管道执行记录做深度分析，"
                "产出经验教训和改进建议，完成后自动通知调用方。无需任何输入参数。"
            ),
            input_schema={"type": "object", "properties": {}},
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.SYSTEM,
            tags=["review", "maintenance", "system"],
            when_to_use=[
                "刚跑完一批任务（尤其出现过失败或反复重试），想沉淀经验教训时",
                "用户明确要求「复盘」「总结经验」「分析失败原因」时",
                "阶段性收尾，想把本次执行的得失固化为可复用知识时",
            ],
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行复盘触发。

        获取父 pipeline_id → 调 maintenance_service.trigger_llm_review() → 返回结果。

        Args:
            inputs: 输入参数

        Returns:
            ToolExecutionResult: 提交结果
        """
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

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

        parent_pipeline_id = _current_pipeline_id.get() or ""
        result = await maintenance_service.trigger_llm_review(
            parent_pipeline_id=parent_pipeline_id,
        )

        status = result.get("status", "submitted")
        message = result.get("message", "复盘任务已提交，完成后会通知您结果。")

        return create_success_result(
            data={"status": status},
            metadata={"message": message},
        )
