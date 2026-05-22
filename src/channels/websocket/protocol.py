"""WebSocket 通信协议定义。

定义 WebSocket 通道的事件类型、数据格式和消息信封，
遵循 frontend-backend-protocol.md 中定义的通信协议。

事件分类：
- 流式输出事件：stream_start / stream_chunk / stream_end / thinking_*
- 工具执行事件：execution_start / execution_progress / execution_done
- 管道状态事件：pipeline_received / pipeline_start / pipeline_end / iteration_start / iteration_end
- 错误事件：plugin_error / pipeline_error
- 控制事件：stop_generation / resume_action / connection_confirmation
- ACK 事件：message_ack（前端确认收到关键消息）
- 重连事件：request_missed / missed_messages（断线重连后获取遗漏消息）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---- 协议版本 ----

PROTOCOL_VERSION = "2.0.0"
"""当前协议版本号。"""

MIN_SUPPORTED_VERSION = "1.0.0"
"""最小兼容协议版本号。"""


def parse_version(version: str) -> tuple[int, int, int]:
    """将语义版本字符串解析为整数元组。

    Args:
        version: 语义版本字符串，格式为 "major.minor.patch"。

    Returns:
        (major, minor, patch) 整数元组。
    """
    parts = version.split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def is_version_compatible(client_version: str) -> bool:
    """检查客户端协议版本是否与服务器兼容。

    当客户端 major 版本号 >= 服务端 MIN_SUPPORTED_VERSION 的 major，
    且 <= 当前 PROTOCOL_VERSION 的 major 时视为兼容。

    Args:
        client_version: 客户端协议版本字符串。

    Returns:
        版本是否兼容。
    """
    try:
        client = parse_version(client_version)
        min_ver = parse_version(MIN_SUPPORTED_VERSION)
        current = parse_version(PROTOCOL_VERSION)
        return min_ver[0] <= client[0] <= current[0]
    except (ValueError, IndexError):
        return False


def negotiate_version(client_version: str) -> str:
    """协商最终使用的协议版本。

    如果客户端版本与当前服务端版本一致，使用当前版本；
    否则使用两者中较低的那个（但不得低于 MIN_SUPPORTED_VERSION）。

    Args:
        client_version: 客户端协议版本字符串。

    Returns:
        协商后的协议版本字符串。
    """
    if client_version == PROTOCOL_VERSION:
        return PROTOCOL_VERSION
    if is_version_compatible(client_version):
        client = parse_version(client_version)
        current = parse_version(PROTOCOL_VERSION)
        if client <= current:
            return client_version
        return PROTOCOL_VERSION
    return PROTOCOL_VERSION


# ---- ACK 默认超时 ----

ACK_TIMEOUT_SECONDS: float = 10.0
"""ACK 确认超时时间（秒）。"""

ACK_MAX_RETRIES: int = 3
"""ACK 确认最大重试次数。"""


# ---- 需要 ACK 确认的事件集合 ----

ACK_REQUIRED_EVENTS: set[str] = {
    "interaction_request",
    "approval_required",
    "approval_request",
    "review_request",
}
"""需要前端 ACK 确认的关键事件类型集合。"""


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
    PIPELINE_RECEIVED = "pipeline_received"
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

    # --- ACK 事件（前端 → 后端）---
    MESSAGE_ACK = "message_ack"

    # --- 审批与工作空间事件 ---
    REVIEW_REQUEST = "review_request"
    REVIEW_STATUS_UPDATE = "review_status_update"
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_UPDATED = "artifact_updated"
    ANNOTATION_ADDED = "annotation_added"
    ANNOTATION_RESOLVED = "annotation_resolved"

    # --- 重连事件（双向）---
    REQUEST_MISSED = "request_missed"
    MISSED_MESSAGES = "missed_messages"


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
        version: 协议版本号（可选，用于版本协商）
        requires_ack: 是否需要前端 ACK 确认（可选）
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    request_id: str = field(
        default_factory=lambda: str(uuid.uuid4()),
    )
    version: str = ""
    requires_ack: bool = False

    def to_dict(self) -> dict[str, Any]:
        """将信封序列化为字典。

        Returns:
            符合协议格式的字典，可直接 JSON 序列化后
            通过 WebSocket 发送。
        """
        result: dict[str, Any] = {
            "type": self.type,
            "data": self.data,
            "timestamp": self.timestamp,
            "request_id": self.request_id,
        }
        if self.version:
            result["version"] = self.version
        if self.requires_ack:
            result["requires_ack"] = True
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventEnvelope:
        """从字典反序列化信封。

        Args:
            data: 包含 type/data/timestamp/request_id
                  的字典。

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
            timestamp=data.get(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            ),
            request_id=data.get(
                "request_id", str(uuid.uuid4()),
            ),
            version=data.get("version", ""),
            requires_ack=data.get("requires_ack", False),
        )


@dataclass
class StreamStartData:
    """stream_start 事件数据。

    Attributes:
        message_id: 消息唯一标识
        model: 使用的 LLM 模型名称
        thinking_enabled: 是否启用思考过程
        pipeline_id: 管道 ID，用于前端消息路由
    """

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model: str = ""
    thinking_enabled: bool = False
    pipeline_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "message_id": self.message_id,
            "model": self.model,
            "thinking_enabled": self.thinking_enabled,
            "pipeline_id": self.pipeline_id,
        }


@dataclass
class StreamChunkData:
    """stream_chunk 事件数据。

    Attributes:
        message_id: 消息唯一标识
        content: 当前 chunk 的文本内容
        sequence: chunk 序号（从 1 开始递增）
        pipeline_id: 管道 ID，用于前端消息路由
    """

    message_id: str
    content: str = ""
    sequence: int = 0
    pipeline_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "message_id": self.message_id,
            "content": self.content,
            "sequence": self.sequence,
            "pipeline_id": self.pipeline_id,
        }


@dataclass
class StreamEndData:
    """stream_end 事件数据。

    Attributes:
        message_id: 消息唯一标识
        full_content: 完整生成内容
        usage: token 使用量信息
        pipeline_id: 管道 ID，用于前端消息路由
    """

    message_id: str
    full_content: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    pipeline_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "message_id": self.message_id,
            "full_content": self.full_content,
            "usage": self.usage,
            "pipeline_id": self.pipeline_id,
        }
        return result


@dataclass
class ExecutionStartData:
    """execution_start 事件数据。

    Attributes:
        execution_id: 执行唯一标识
        tool_name: 工具名称
        params: 工具参数
        parent_id: 父执行 ID（可选，用于嵌套调用）
        pipeline_id: 管道 ID，用于前端消息路由
    """

    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    pipeline_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        result: dict[str, Any] = {
            "execution_id": self.execution_id,
            "tool_name": self.tool_name,
            "params": self.params,
            "pipeline_id": self.pipeline_id,
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
class PipelineReceivedData:
    """pipeline_received 事件数据。

    后端收到用户消息后立即发送，告知前端消息已进入处理管道。

    Attributes:
        pipeline_id: 管道 ID
        thread_id: 线程 ID
        user_message_id: 用户消息 ID
    """

    pipeline_id: str
    thread_id: str = ""
    user_message_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，仅包含非空字段。"""
        result: dict[str, Any] = {"pipeline_id": self.pipeline_id}
        if self.thread_id:
            result["thread_id"] = self.thread_id
        if self.user_message_id:
            result["user_message_id"] = self.user_message_id
        return result


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
        version: 服务端协商后的协议版本
    """

    session_id: str
    thread_id: str = ""
    status: str = "connected"
    version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "version": self.version,
        }


@dataclass
class MessageAckData:
    """message_ack 事件数据（前端 → 后端）。

    前端收到 requires_ack=True 的消息后，发送 ACK 确认。

    Attributes:
        request_id: 被确认的消息的 request_id
        received_at: 前端确认收到的时间戳
    """

    request_id: str
    received_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "request_id": self.request_id,
            "received_at": self.received_at,
        }


@dataclass
class RequestMissedData:
    """request_missed 事件数据（前端 → 后端）。

    重连后前端请求获取断线期间遗漏的消息。

    Attributes:
        last_received_request_id: 前端最后收到的
            消息 request_id（空字符串表示从连接
            建立开始获取）
    """

    last_received_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "last_received_request_id": (
                self.last_received_request_id
            ),
        }


@dataclass
class MissedMessagesData:
    """missed_messages 事件数据（后端 → 前端）。

    后端响应 request_missed，返回遗漏的消息列表。

    Attributes:
        messages: 遗漏的消息列表（EventEnvelope 序列化）
        total: 总遗漏消息数量
        has_more: 是否还有更多未发送的消息
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "messages": self.messages,
            "total": self.total,
            "has_more": self.has_more,
        }


def create_event(
    event_type: EventType,
    data: dict[str, Any] | None = None,
    request_id: str | None = None,
    version: str = "",
    requires_ack: bool = False,
) -> EventEnvelope:
    """创建事件信封的便捷工厂函数。

    Args:
        event_type: 事件类型
        data: 事件数据字典
        request_id: 请求 ID（可选，不传则自动生成）
        version: 协议版本号（可选）
        requires_ack: 是否需要 ACK 确认

    Returns:
        封装好的 EventEnvelope 实例。
    """
    return EventEnvelope(
        type=event_type.value,
        data=data or {},
        request_id=request_id or str(uuid.uuid4()),
        version=version,
        requires_ack=requires_ack,
    )
