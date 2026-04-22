"""WebSocket 通信协议定义。

定义 WebSocket 通道的事件类型、数据格式和消息信封，
遵循 frontend-backend-protocol.md 中定义的通信协议。

事件分类：
- 流式输出事件：stream_start / stream_chunk / stream_end / thinking_*
- 工具执行事件：execution_start / execution_progress / execution_done
- 管道状态事件：pipeline_start / pipeline_end / iteration_start / iteration_end
- 错误事件：plugin_error / pipeline_error
- 控制事件：stop_generation / resume_action / connection_confirmation
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """WebSocket 事件类型枚举。

    按协议文档定义的事件类型，用于 type 字段。
    """

    # --- 连接事件 ---
    CONNECTION_CONFIRMATION = "connection_confirmation"

    # --- 流式输出事件 ---
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_chunk"
    STREAM_END = "stream_end"
    THINKING_START = "thinking_start"
    THINKING_CHUNK = "thinking_chunk"
    THINKING_END = "thinking_end"

    # --- 工具执行事件 ---
    EXECUTION_START = "execution_start"
    EXECUTION_PROGRESS = "execution_progress"
    EXECUTION_DONE = "execution_done"

    # --- 管道状态事件 ---
    PIPELINE_START = "pipeline_start"
    PIPELINE_END = "pipeline_end"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"

    # --- 错误事件 ---
    PLUGIN_ERROR = "plugin_error"
    PIPELINE_ERROR = "pipeline_error"

    # --- 控制事件（前端 → 后端）---
    USER_INPUT = "user_input"
    STOP_GENERATION = "stop_generation"
    RESUME_ACTION = "resume_action"


class ControlCommand(str, Enum):
    """控制命令枚举。

    前端发送的控制命令类型。
    """

    STOP_GENERATION = "stop_generation"
    RESUME_ACTION = "resume_action"


@dataclass
class EventEnvelope:
    """WebSocket 消息信封。

    所有 WebSocket 消息的统一格式，确保前后端通信一致性。

    Attributes:
        type: 事件类型
        data: 事件数据
        timestamp: ISO 8601 时间戳
        request_id: 请求唯一标识，用于关联请求/响应
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        """将信封序列化为字典。

        Returns:
            符合协议格式的字典，可直接 JSON 序列化后通过 WebSocket 发送。
        """
        return {
            "type": self.type,
            "data": self.data,
            "timestamp": self.timestamp,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventEnvelope:
        """从字典反序列化信封。

        Args:
            data: 包含 type/data/timestamp/request_id 的字典。

        Returns:
            EventEnvelope 实例。

        Raises:
            ValueError: 缺少 type 字段。
        """
        if "type" not in data:
            raise ValueError("Event envelope must have 'type' field")
        return cls(
            type=data["type"],
            data=data.get("data", {}),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            request_id=data.get("request_id", str(uuid.uuid4())),
        )


@dataclass
class StreamStartData:
    """stream_start 事件数据。

    Attributes:
        message_id: 消息唯一标识
        model: 使用的 LLM 模型名称
        thinking_enabled: 是否启用思考过程
    """

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model: str = ""
    thinking_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "message_id": self.message_id,
            "model": self.model,
            "thinking_enabled": self.thinking_enabled,
        }


@dataclass
class StreamChunkData:
    """stream_chunk 事件数据。

    Attributes:
        message_id: 消息唯一标识
        content: 当前 chunk 的文本内容
        sequence: chunk 序号（从 1 开始递增）
    """

    message_id: str
    content: str = ""
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "message_id": self.message_id,
            "content": self.content,
            "sequence": self.sequence,
        }


@dataclass
class StreamEndData:
    """stream_end 事件数据。

    Attributes:
        message_id: 消息唯一标识
        full_content: 完整生成内容
        usage: token 使用量信息
    """

    message_id: str
    full_content: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "message_id": self.message_id,
            "full_content": self.full_content,
            "usage": self.usage,
        }


@dataclass
class ExecutionStartData:
    """execution_start 事件数据。

    Attributes:
        execution_id: 执行唯一标识
        tool_name: 工具名称
        params: 工具参数
        parent_id: 父执行 ID（可选，用于嵌套调用）
    """

    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "params": self.params,
        }
        if self.parent_id is not None:
            result["parent_id"] = self.parent_id
        return result


@dataclass
class ExecutionProgressData:
    """execution_progress 事件数据。

    Attributes:
        execution_id: 执行唯一标识
        progress: 进度百分比（0-100）
        message: 进度描述
        partial_output: 部分输出
    """

    execution_id: str
    progress: float = 0.0
    message: str | None = None
    partial_output: Any = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "execution_id": self.execution_id,
            "progress": self.progress,
        }
        if self.message is not None:
            result["message"] = self.message
        if self.partial_output is not None:
            result["partial_output"] = self.partial_output
        return result


@dataclass
class ExecutionDoneData:
    """execution_done 事件数据。

    Attributes:
        execution_id: 执行唯一标识
        status: 执行状态（"success" / "error"）
        result: 执行结果
        duration: 执行时长（秒）
    """

    execution_id: str
    status: str = "success"
    result: Any = None
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "result": self.result,
            "duration": self.duration,
        }


@dataclass
class PipelineStartData:
    """pipeline_start 事件数据。

    Attributes:
        session_id: 会话唯一标识
        agent_level: Agent 层级
        config: 管道配置信息
    """

    session_id: str
    agent_level: str = "L1"
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "session_id": self.session_id,
            "agent_level": self.agent_level,
            "config": self.config,
        }


@dataclass
class PipelineEndData:
    """pipeline_end 事件数据。

    Attributes:
        session_id: 会话唯一标识
        status: 结束状态（"completed" / "failed" / "stopped"）
        total_iterations: 总迭代次数
        total_duration: 总执行时长（秒）
    """

    session_id: str
    status: str = "completed"
    total_iterations: int = 0
    total_duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "session_id": self.session_id,
            "status": self.status,
            "total_iterations": self.total_iterations,
            "total_duration": self.total_duration,
        }


@dataclass
class ErrorData:
    """错误事件数据（plugin_error / pipeline_error 共用）。

    Attributes:
        error: 错误信息
        phase: 出错阶段（"input" / "core" / "output" / "router"）
        plugin: 出错插件名称（可选）
        policy: 错误策略（可选）
        fallback: 降级信息（可选）
    """

    error: str
    phase: str = ""
    plugin: str | None = None
    policy: str | None = None
    fallback: Any = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "error": self.error,
            "phase": self.phase,
        }
        if self.plugin is not None:
            result["plugin"] = self.plugin
        if self.policy is not None:
            result["policy"] = self.policy
        if self.fallback is not None:
            result["fallback"] = self.fallback
        return result


@dataclass
class ConnectionConfirmationData:
    """connection_confirmation 事件数据。

    Attributes:
        session_id: 会话唯一标识
        thread_id: 线程 ID（前端传入，用于恢复会话）
        status: 连接状态
    """

    session_id: str
    thread_id: str = ""
    status: str = "connected"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "status": self.status,
        }


def create_event(
    event_type: EventType,
    data: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> EventEnvelope:
    """创建事件信封的便捷工厂函数。

    Args:
        event_type: 事件类型
        data: 事件数据字典
        request_id: 请求 ID（可选，不传则自动生成）

    Returns:
        封装好的 EventEnvelope 实例。
    """
    return EventEnvelope(
        type=event_type.value,
        data=data or {},
        request_id=request_id or str(uuid.uuid4()),
    )
