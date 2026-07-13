"""管道检查点模块。

提供检查点的保存与恢复功能，将管道运行状态持久化以便后续恢复执行。
本模块从 PipelineEngine 中抽离，作为独立的模块级公开函数供外部调用或委托调用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pipeline.types import StateKeys

if TYPE_CHECKING:
    from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager

logger = logging.getLogger(__name__)


async def save_checkpoint(
    checkpoint_manager: PipelineCheckpointManager | None,
    suspended_state: dict[str, Any] | None,
    pipeline_id: str,
    phase: str = "manual",
) -> str | None:
    """手动保存管道检查点。

    将当前管道的挂起状态通过检查点管理器持久化保存。

    Args:
        checkpoint_manager: 检查点管理器实例，为 None 时直接返回 None
        suspended_state: 管道挂起时的状态字典，为 None 或空字典时不保存
        pipeline_id: 管道唯一标识，用于检查点管理器的存储索引
        phase: 检查点阶段标记，如 "manual"、"auto"、"suspended"

    Returns:
        保存成功时返回检查点 ID 字符串；无检查点管理器或无状态可保存时返回 None
    """
    if checkpoint_manager is None:
        logger.warning("No checkpoint manager configured")
        return None

    current_state = suspended_state or {}
    if not current_state:
        logger.warning("No state to checkpoint")
        return None

    pid = current_state.get(StateKeys.PIPELINE_ID, pipeline_id)
    try:
        checkpoint_id = await checkpoint_manager.save(pid, current_state, phase=phase)
        logger.info("Checkpoint saved: %s (phase=%s)", checkpoint_id, phase)
        return checkpoint_id
    except Exception as exc:
        logger.error("Failed to save checkpoint: %s", exc)
        return None


async def restore_from_checkpoint(
    checkpoint_manager: PipelineCheckpointManager | None,
    checkpoint_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """从检查点恢复管道状态。

    通过检查点管理器加载指定 ID 的检查点数据，并返回其中保存的状态字典。

    Args:
        checkpoint_manager: 检查点管理器实例，为 None 时直接返回失败
        checkpoint_id: 待恢复的检查点唯一标识

    Returns:
        二元组 (是否恢复成功, 恢复后的状态字典)。
        恢复成功时状态字典非空；失败时状态字典为 None。
    """
    if checkpoint_manager is None:
        logger.warning("No checkpoint manager configured")
        return False, None

    try:
        data = await checkpoint_manager.load(checkpoint_id)
        if data is None:
            logger.error("Checkpoint not found: %s", checkpoint_id)
            return False, None

        state: dict[str, Any] = data.get("state", {})
        logger.info(
            "Restored from checkpoint %s (iteration=%d)",
            checkpoint_id,
            state.get(StateKeys.ITERATION, 0),
        )
        return True, state
    except Exception as exc:
        logger.error("Failed to restore from checkpoint: %s", exc)
        return False, None
