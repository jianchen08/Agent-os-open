"""管道调度任务数据类。

包装 PipelineEngine + initial_state，供调度器统一调度执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineTask:
    """管道调度任务。

    将 PipelineEngine 和其初始状态打包为调度单元，
    供 Scheduler 统一管理优先级和并发。

    Attributes:
        pipeline_id: 管道实例唯一标识
        engine: PipelineEngine 实例（用 Any 避免循环引用）
        initial_state: 管道初始状态字典
        priority: 调度优先级，数值越小优先级越高
    """

    pipeline_id: str
    engine: Any  # PipelineEngine，用 Any 避免循环引用
    initial_state: dict[str, Any]
    priority: int = 5
