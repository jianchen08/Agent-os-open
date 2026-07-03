"""管道消息类型定义。



定义管道消息的标准数据结构和枚举类型。

所有进入管道的消息必须构造为 PipelineMessage 对象，

前端原始 JSON 不可直接透传到管道。

"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.types import AgentConfig
    from pipeline.sink import IOutputSink


class MessageSource(StrEnum):
    """消息来源标识。"""

    USER = "user"

    SYSTEM = "system"

    TRIGGER = "trigger"

    AGENT = "agent"

    INTERACTION = "interaction"


class MessageType(StrEnum):
    """消息类型。"""

    CHAT = "chat"  # 普通对话消息

    CONTROL = "control"  # 控制命令（stop_generation / resume_action）

    INTERACTION_RESPONSE = "interaction_response"  # 人机交互响应

    NOTIFICATION = "notification"  # 系统通知


@dataclass
class PipelineMessage:
    """管道注入的标准消息对象。



    所有进入管道的消息（无论来源）都必须构造为此对象。

    前端原始 JSON 不可直接透传到管道。



    Attributes:

        type: 消息类型

        content: 消息文本内容（经过校验和清洗）

        source: 消息来源

        pipeline_id: 目标管道 ID（路由键）

        thread_id: 关联的 WebSocket thread_id

        client_message_id: 前端消息 ID（用于关联确认）

        metadata: 扩展元数据

        attachments: 附件列表（图片/文件等，预留）

    """

    type: MessageType

    content: str

    source: MessageSource = MessageSource.USER

    pipeline_id: str = ""

    thread_id: str = ""

    client_message_id: str = ""

    metadata: dict[str, Any] = field(default_factory=dict)

    attachments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """消息内容是否为空。"""

        return not self.content or not self.content.strip()


@dataclass
class PipelineRequest:
    """管道注入请求参数。



    封装 send_pipeline_message 的所有参数，

    按场景构造不同字段子集。



    Attributes:

        message: 标准消息对象（必须）

        agent_config: Agent 配置（revive 场景需要）

        conversation_history: 对话历史（revive 场景需要）

        output_sink: 输出目标（可选，自动创建）

        streaming: 是否流式输出

        workspace: 工作目录

        task_id: 关联任务 ID

    """

    message: PipelineMessage

    agent_config: AgentConfig | None = None

    conversation_history: list[dict] | None = None

    output_sink: IOutputSink | None = None

    streaming: bool = True

    workspace: str = ""

    task_id: str = ""
