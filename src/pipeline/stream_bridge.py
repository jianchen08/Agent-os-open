"""管道流式事件桥接模块（re-export 入口）。

engine 主动 emit 事件到 bridge，bridge 格式化后通过 IOutputSink 推送到前端。

Phase 1 改造：删除 drain_loop 独立协程，engine 直接调 emit_* 推送。

实现已拆分到子模块：
- sink.py: 输出目标抽象层（IOutputSink, TargetedSink, MultiChannelSink, EnvelopeSource）
- bridge_core.py: 桥接器核心状态管理 + emit 接口
- bridge_events.py: 事件格式化处理（_handle_chunk）
- bridge_drain.py: 统一出口函数（send_frontend_event）

本文件保持所有公共 API 的导入路径不变，确保外部模块无需修改。
"""

from __future__ import annotations

from pipeline.bridge_core import BridgeCore
from pipeline.bridge_drain import send_frontend_event
from pipeline.bridge_events import BridgeEventsMixin

# Re-export 所有公共 API
from pipeline.sink import (
    EnvelopeSource,
    IOutputSink,
    MultiChannelSink,
    TargetedSink,
    create_targeted_sink,
)


class PipelineStreamBridge(BridgeEventsMixin, BridgeCore):
    """管道流式事件桥接器（无状态转发器）。

    Phase 1 改造：删除 drain_loop 独立协程，engine 主动调 emit_* 推送。

    核心职责：
    1. 提供 emit_start/chunk/finish/suspend/error 接口供 engine 调用
    2. 从 state.raw_result 读取完整内容推送（单一数据源）
    3. 维护 thinking 状态追踪、tool_start 去重（不累加内容）
    """

    def __init__(
        self,
        pipeline_id: str,
        output_sink: IOutputSink,
        message_id: str | None = None,
    ) -> None:
        """初始化管道流式桥接器。

        Args:
            pipeline_id: 管道 ID，附加到每个事件的 data 中
            output_sink: 输出目标，负责实际发送事件
            message_id: 消息 ID（hex 格式），不传则自动生成
        """
        self._init_core_state(pipeline_id, output_sink, message_id)


__all__ = [
    "EnvelopeSource",
    "IOutputSink",
    "TargetedSink",
    "MultiChannelSink",
    "PipelineStreamBridge",
    "send_frontend_event",
    "create_targeted_sink",
]
