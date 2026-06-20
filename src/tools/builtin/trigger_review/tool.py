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

                "properties": {},

            },

            source=ToolSource.CODE,

            category=ToolCategory.SYSTEM,

            level=ToolLevel.SYSTEM,

            tags=["review", "maintenance", "system"],

        )



    async def execute(self, inputs: dict[str, Any]):

        """执行复盘触发。



        流程与 TaskExecutor 一致：

        1. 从 AgentRegistry 获取 review_agent 配置

        2. 通过 EngineRegistry 注册管道

        3. 用 message_bus.emit 注入复盘上下文



        Args:

            inputs: 输入参数



        Returns:

            ToolExecutionResult: 提交结果

        """

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



        maintenance_service._review_running = True



        async def _run() -> None:

            """触发复盘执行。



            优先尝试注册 review_agent 管道做 LLM 深度分析；若 review_agent

            不可用（配置缺失或 LLM 不可达），降级到 ReviewEngine 直接复盘——

            后者不依赖 LLM，已验证能基于 summary.error/record.error 产出经验。

            保证"工具触发复盘"在任何环境下都能产出具体成果，而非静默放弃。

            """

            try:

                # 路径1（优先）：尝试 LLM 深度复盘管道

                reviewed_via_llm = await _try_llm_review()

                if not reviewed_via_llm:

                    # 路径2（保底）：直接走 ReviewEngine，不依赖 LLM/agent 配置

                    result = await maintenance_service.trigger_review_now()

                    logger.info(

                        "[TriggerReview] 直接复盘完成 (reviewed=%d, experiences=%d)",

                        result.get("pipelines_reviewed", 0),

                        result.get("experiences_saved", 0),

                    )

            except Exception as exc:

                logger.error("[TriggerReview] 复盘执行失败: %s", exc, exc_info=True)

            finally:

                maintenance_service._review_running = False



        async def _try_llm_review() -> bool:

            """尝试通过 review_agent 管道做 LLM 深度复盘。



            Returns:

                True 表示已成功提交 LLM 复盘管道；False 表示 review_agent

                不可用（配置缺失或运行时组件缺失），调用方应降级到直接复盘。

            """

            try:

                from agents.loader import AgentConfigLoader



                from tools.tool_context import emit

                from tools.tool_context import get_engine_registry

            except ImportError as exc:

                logger.debug("[TriggerReview] LLM 复盘组件不可用: %s", exc)

                return False



            # 加载 review_agent 配置：通过全局单例 registry 查询

            agent_config = None

            try:

                from agents.global_registry import get_global_agent_registry_sync



                agent_config = get_global_agent_registry_sync().get(REVIEW_AGENT_ID)

            except Exception:

                pass

            if agent_config is None:

                logger.info(

                    "[TriggerReview] review_agent 配置不存在，降级到直接复盘"

                )

                return False



            try:

                from infrastructure.service_provider import get_service_provider



                registry = get_engine_registry()

                _provider = get_service_provider()

                entry = registry.register_pipeline(

                    tags={"source": "tool_review"},

                    input_route_table=_provider.get("input_route_table"),

                    output_route_table=_provider.get("output_route_table"),

                    plugin_registry=_provider.get("plugin_registry"),

                    services=_provider.get_all_services(),

                )

                if entry is None:

                    logger.warning(

                        "[TriggerReview] 管道注册失败（缺少路由表/插件注册表），降级到直接复盘"

                    )

                    return False

                pipeline_id = entry.engine.pipeline_id



                from tools.tool_context import MessageType, PipelineMessage

                _review_msg = PipelineMessage(

                    type=MessageType.CHAT,

                    content="[工具触发复盘] 请分析最近的管道执行记录，产出经验和改进建议。",

                    pipeline_id=pipeline_id,

                    metadata={"source": "tool_review"},

                )

                result = await emit(

                    _review_msg,

                    agent_config=agent_config,

                )

                logger.info(

                    "[TriggerReview] LLM 复盘管道已提交 (pipeline=%s, success=%s)",

                    pipeline_id, result.success,

                )

                return bool(result.success)

            except Exception as exc:

                logger.warning(

                    "[TriggerReview] LLM 复盘管道提交失败，降级到直接复盘: %s", exc

                )

                return False



        asyncio.create_task(_run())



        return create_success_result(

            data={"status": "submitted"},

            metadata={"message": "复盘任务已提交，完成后会通知您结果。"},

        )

