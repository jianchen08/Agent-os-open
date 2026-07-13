"""经验沉淀输出插件。

在任务完成时，自动从 ChunkData（压缩块）提炼可检索的知识。
触发条件：state 中 task_complete=True 或 execution_status="completed"。

数据流：
  context_window_guard 产出 ChunkData → 本插件读取 → 提炼 Knowledge → 存储

通过 ctx.get_service("chunk_service") 获取 ChunkService，
通过 ctx.get_service("knowledge_service") 获取 KnowledgeService。

State 命名空间：
    - experience_consolidated : 是否沉淀成功
    - knowledge_id : 生成的知识 ID
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ExperienceConsolidatorPlugin(IOutputPlugin):
    """经验沉淀输出插件。

    在任务完成时，从 ChunkData（压缩块）提炼知识并存储。
    触发条件：state 中 task_complete=True 或 execution_status="completed"。

    通过 ctx.get_service("chunk_service") 获取 ChunkService，
    通过 ctx.get_service("knowledge_service") 获取 KnowledgeService。

    优先级：28（在 context_compress 之后）
    错误策略：SKIP（沉淀失败不影响当轮结果）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化经验沉淀插件。

        Args:
            config: 插件配置，当前无特殊配置项
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "experience_consolidator"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 28

    @property
    def route_signals(self) -> list[str]:
        """本插件关注所有路由信号。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:  # noqa: PLR0911
        """执行经验沉淀逻辑。

        检查任务是否完成，如果完成则从 ChunkData 提炼知识并存储。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含沉淀结果的输出结果
        """
        # 1. 检查任务完成状态
        task_complete = ctx.state.get(StateKeys.TASK_COMPLETE, False)
        execution_status = ctx.state.get(StateKeys.EXECUTION_STATUS, "")
        if not task_complete and execution_status != "completed":
            return OutputResult()

        # 2. 获取 pipeline_run_id
        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            logger.debug(
                "[%s] 无 pipeline_run_id，跳过经验沉淀",
                self.name,
            )
            return OutputResult()

        # 3. 获取服务实例
        try:
            chunk_service = ctx.get_service("chunk_service")
        except KeyError:
            logger.debug(
                "[%s] chunk_service 不可用，跳过经验沉淀",
                self.name,
            )
            return OutputResult()

        try:
            knowledge_service = ctx.get_service("knowledge_service")
        except KeyError:
            logger.debug(
                "[%s] knowledge_service 不可用，跳过经验沉淀",
                self.name,
            )
            return OutputResult()

        # 4. 从 ChunkService 获取当前管道的所有压缩块
        try:
            chunks = await chunk_service.find_by_session(pipeline_run_id)
        except Exception as e:
            logger.warning(
                "[%s] 获取压缩块失败 | pipeline_run_id=%s | error=%s",
                self.name,
                pipeline_run_id,
                e,
            )
            return OutputResult()

        if not chunks:
            logger.debug(
                "[%s] 无压缩块可沉淀 | pipeline_run_id=%s",
                self.name,
                pipeline_run_id,
            )
            return OutputResult()

        # 5. 从压缩块提炼知识
        user_id = ctx.state.get("user_id", "")
        knowledge_ids: list[str] = []
        errors: list[str] = []

        for chunk in chunks:
            content = self._extract_knowledge_content(chunk)
            if not content.strip():
                continue

            keywords = getattr(chunk, "keywords", []) or []
            extra_data = {
                "source_chunk_id": chunk.id,
                "layer": chunk.layer,
                "keywords": keywords,
                "token_count": chunk.token_count,
                "pipeline_run_id": pipeline_run_id,
            }

            try:
                result = await knowledge_service.create_knowledge(
                    user_id=user_id,
                    content=content,
                    source_type="experience",
                    extra_data=extra_data,
                )
                kid = result.get("id", "")
                if kid:
                    knowledge_ids.append(kid)
            except Exception as e:
                errors.append(str(e))
                logger.warning(
                    "[%s] 知识存储失败 | chunk_id=%s | error=%s",
                    self.name,
                    chunk.id,
                    e,
                )

        # 6. 更新 state
        if knowledge_ids:
            logger.info(
                "[%s] 经验沉淀成功 | pipeline_run_id=%s | chunks=%d | knowledge_ids=%s",
                self.name,
                pipeline_run_id,
                len(chunks),
                knowledge_ids,
            )
            return OutputResult(
                state_updates={
                    "experience_consolidated": True,
                    "knowledge_id": knowledge_ids[-1],
                    "knowledge_ids": knowledge_ids,
                },
            )
        logger.debug(
            "[%s] 经验沉淀失败 | pipeline_run_id=%s | chunks=%d | errors=%d",
            self.name,
            pipeline_run_id,
            len(chunks),
            len(errors),
        )
        return OutputResult(
            state_updates={
                "experience_consolidated": False,
            },
        )

    @staticmethod
    def _extract_knowledge_content(chunk: Any) -> str:
        """从 ChunkData 提炼知识内容。

        将压缩块的 content 和 keywords 组合为知识文本。

        Args:
            chunk: ChunkData 实例

        Returns:
            拼接后的知识内容字符串
        """
        parts: list[str] = []

        content = getattr(chunk, "content", "")
        if content:
            parts.append(content)

        keywords = getattr(chunk, "keywords", []) or []
        if keywords:
            parts.append(f"关键词: {', '.join(keywords)}")

        layer = getattr(chunk, "layer", "")
        if layer:
            parts.append(f"层级: {layer}")

        return "\n".join(parts)
