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

REVIEW_AGENT_ID = "review_agent"


class TriggerReviewTool(BuiltinTool):
    """复盘触发工具。

    通过管道引擎注册复盘 Agent 管道，再用消息注入触发执行。
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
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行复盘触发。

        流程与 TaskExecutor 一致：
        1. 从 AgentRegistry 获取 review_agent 配置
        2. 通过 EngineRegistry 注册管道
        3. 用 send_pipeline_message 注入复盘上下文

        Args:
            inputs: 输入参数，含 force

        Returns:
            ToolExecutionResult: 提交结果
        """
        force = inputs.get("force", False)

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
                metadata={
                    "message": (
                        f"当前不满足触发条件（待复盘记录 {pending} 条），"
                        "如需强制触发请设置 force=true"
                    ),
                },
            )

        maintenance_service._review_running = True

        async def _run() -> None:
            """注册复盘管道并通过消息注入触发执行。"""
            try:
                from pipeline.message_bus import send_pipeline_message
                from pipeline.registry import get_engine_registry
                from config.agent_loader import load_agent_config

                agent_config = load_agent_config(REVIEW_AGENT_ID)
                if agent_config is None:
                    from agents.agent_registry import get_agent_registry
                    agent_config = get_agent_registry().get(REVIEW_AGENT_ID)
                if agent_config is None:
                    logger.error("[TriggerReview] Agent '%s' 配置不存在", REVIEW_AGENT_ID)
                    return

                registry = get_engine_registry()
                entry = registry.register_pipeline(
                    tags={"source": "tool_review", "force": str(force)},
                )
                pipeline_id = entry.engine.pipeline_id

                result = await send_pipeline_message(
                    pipeline_id,
                    "[工具触发复盘] 请分析最近的管道执行记录，产出经验和改进建议。",
                    agent_config=agent_config,
                    metadata={"source": "tool_review", "force": force},
                )
                logger.info(
                    "[TriggerReview] 复盘管道已提交 (pipeline=%s, success=%s)",
                    pipeline_id, result.success,
                )
            except Exception as exc:
                logger.error("[TriggerReview] 复盘管道执行失败: %s", exc)
            finally:
                maintenance_service._review_running = False

        asyncio.create_task(_run())

        return create_success_result(
            data={"status": "submitted"},
            metadata={"message": "复盘任务已提交，完成后会通知您结果。"},
        )
