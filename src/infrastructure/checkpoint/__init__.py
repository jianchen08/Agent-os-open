"""管道检查点模块。

提供管道级别的状态快照持久化、历史恢复和会话连续性能力。
与 isolation/checkpoint.py 的文件级检查点不同，本模块专注于
管道运行时状态的保存与恢复。
"""

from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager
from infrastructure.checkpoint.recovery import PipelineRecovery

__all__ = [
    "PipelineCheckpointManager",
    "PipelineRecovery",
]
