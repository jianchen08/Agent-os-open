"""管道恢复服务。

负责从检查点恢复管道执行状态，支持：
- 恢复管道状态快照（自动合并 Agent 配置）
- 恢复并继续执行管道
- 获取恢复信息和恢复建议

恢复流程：
1. 从 checkpoint 加载动态状态（messages、iteration 等）
2. 从 Agent 配置文件加载静态配置（system_prompt、constraints 等）
3. 合并为完整的管道 state
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager

if TYPE_CHECKING:
    from pipeline.engine import PipelineEngine

logger = logging.getLogger(__name__)


class PipelineRecovery:
    """管道恢复服务。

    利用 PipelineCheckpointManager 提供的检查点能力，
    实现管道状态恢复、恢复后继续执行以及恢复信息查询。

    恢复时自动从 Agent 配置文件重新加载静态配置（system_prompt、
    constraints、tool_ids 等），与检查点中的动态状态合并为完整 state。

    Attributes:
        checkpoint_manager: 检查点管理器实例
    """

    def __init__(
        self,
        checkpoint_manager: PipelineCheckpointManager,
    ) -> None:
        """初始化管道恢复服务。

        Args:
            checkpoint_manager: 检查点管理器实例
        """
        self.checkpoint_manager = checkpoint_manager

    async def recover(self, pipeline_id: str) -> dict[str, Any] | None:
        """恢复管道执行状态（动态状态 + Agent 配置合并）。

        从最新检查点加载动态状态，再从 Agent 配置文件加载静态配置，
        合并后返回完整的管道 state。

        优先使用检查点中保存的 agent_config_id 恢复原始 Agent 配置，
        仅当无法按 ID 查找时才回退到默认 Agent（并记录警告）。

        Args:
            pipeline_id: 管道 ID

        Returns:
            恢复的完整状态字典，无可用检查点时返回 None
        """
        checkpoint_data = await self.checkpoint_manager.get_latest(pipeline_id)
        if checkpoint_data is None:
            logger.warning("No checkpoint found for pipeline: %s", pipeline_id)
            return None

        saved_state = checkpoint_data.get("state", {})
        metadata = checkpoint_data.get("metadata", {})

        # FIND-3 fix: 优先使用检查点中保存的 agent_config_id 恢复原始 Agent
        agent_config_id = saved_state.get("agent_config_id")
        base_state = self._load_agent_base_state(agent_config_id=agent_config_id)

        full_state = {**base_state, **saved_state}

        logger.info(
            "Pipeline state recovered: pipeline_id=%s, checkpoint_id=%s, phase=%s, iteration=%d, merged_keys=%s",
            pipeline_id,
            metadata.get("checkpoint_id"),
            metadata.get("phase"),
            metadata.get("iteration", 0),
            list(full_state.keys()),
        )
        return full_state

    async def recover_and_resume(
        self,
        pipeline_id: str,
        engine: PipelineEngine,
    ) -> dict[str, Any] | None:
        """恢复管道状态并继续执行。

        从最新检查点加载状态，注入到 PipelineEngine 的 _suspended_state，
        然后调用 engine.resume() 继续执行管道循环。

        Args:
            pipeline_id: 管道 ID
            engine: 目标 PipelineEngine 实例

        Returns:
            管道最终状态字典，恢复失败时返回 None

        Raises:
            RuntimeError: 检查点不存在时抛出
        """
        state = await self.recover(pipeline_id)
        if state is None:
            raise RuntimeError(f"No checkpoint to recover for pipeline: {pipeline_id}")

        logger.info(
            "Pipeline resuming from checkpoint: pipeline_id=%s, iteration=%d",
            pipeline_id,
            state.get("iteration", 0),
        )

        final_state = await engine.resume_from_state(state)
        return final_state

    async def get_recovery_info(self, pipeline_id: str) -> dict[str, Any] | None:
        """获取管道恢复信息。

        查询最新检查点的元数据，并附加恢复建议。

        Args:
            pipeline_id: 管道 ID

        Returns:
            恢复信息字典，包含 checkpoint 元数据和 recovery_suggestion；
            无检查点时返回 None
        """
        checkpoint_data = await self.checkpoint_manager.get_latest(pipeline_id)
        if checkpoint_data is None:
            return None

        metadata = checkpoint_data.get("metadata", {})
        state = checkpoint_data.get("state", {})

        phase = metadata.get("phase", "unknown")
        suggestion = self._build_suggestion(phase, metadata)

        return {
            "checkpoint": metadata,
            "state_keys": list(state.keys()) if isinstance(state, dict) else [],
            "recovery_suggestion": suggestion,
        }

    def _load_agent_base_state(
        self,
        agent_config_id: str | None = None,
    ) -> dict[str, Any]:
        """从 Agent 配置文件加载静态基础状态。

        FIND-3 fix: 优先使用 agent_config_id 从 agent_registry 查找原始 Agent；
        找不到原始 Agent 配置则返回空状态，禁止静默回退到默认 Agent（灵汐）。

        Args:
            agent_config_id: 检查点中保存的原始 Agent 配置 ID（可选）

        Returns:
            Agent 配置的状态字典，加载失败时返回空字典
        """
        # 优先按 ID 查找原始 Agent
        if agent_config_id:
            try:
                from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

                provider = get_service_provider()
                agent_registry = provider.get("agent_registry")
                if agent_registry:
                    agent_config = agent_registry.get(agent_config_id)
                    if agent_config and hasattr(agent_config, "to_state"):
                        base_state = agent_config.to_state()
                        logger.info(
                            "Agent base state loaded by config_id=%s, keys=%s",
                            agent_config_id,
                            list(base_state.keys()),
                        )
                        return base_state
                    logger.warning(
                        "agent_config_id=%s 在 registry 中未找到，将返回空状态（禁止静默回退到默认 Agent）",
                        agent_config_id,
                    )
            except Exception as exc:
                logger.warning(
                    "按 agent_config_id=%s 加载失败，将返回空状态（禁止静默回退到默认 Agent）: %s",
                    agent_config_id,
                    exc,
                )

        # 找不到原始 Agent 配置，返回空字典（禁止静默回退到默认 Agent）
        logger.warning(
            "agent_config_id=%s 未找到，返回空状态（禁止静默回退到默认 Agent）",
            agent_config_id,
        )
        return {}

    def _build_suggestion(self, phase: str, metadata: dict[str, Any]) -> str:
        """根据检查点阶段生成恢复建议。

        Args:
            phase: 检查点保存时的阶段标记
            metadata: 检查点元数据

        Returns:
            恢复建议描述字符串
        """
        iteration = metadata.get("iteration", 0)

        if phase == "suspended":
            return (
                f"管道在第 {iteration} 轮迭代时被挂起（suspended）。建议使用 recover_and_resume 从挂起点恢复继续执行。"
            )
        if phase == "auto":
            return (
                f"管道在第 {iteration} 轮迭代时自动保存了检查点。"
                "可以使用 recover 恢复状态，或 recover_and_resume 恢复并继续。"
            )
        if phase.startswith("pre_"):
            return f"管道在 {phase} 阶段（第 {iteration} 轮）保存了检查点。建议从该阶段入口处重新执行。"
        if phase.startswith("post_"):
            return f"管道在 {phase} 阶段（第 {iteration} 轮）保存了检查点。建议从下一阶段入口处恢复执行。"
        return f"管道在第 {iteration} 轮迭代时保存了检查点（phase={phase}）。可以使用 recover 恢复状态后手动处理。"
