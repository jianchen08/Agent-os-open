"""
Thread/Session 相关的数据模型和 Schema 定义
"""

from typing import Any

from pydantic import BaseModel, Field


class ThreadCreateRequest(BaseModel):
    """创建线程请求

    创建会话时必须指定绑定的 Agent ID，不允许创建没有 Agent 的会话。
    """

    intent: str | None = Field(None, description="用户意图/标题")
    agent_id: str = Field(..., description="绑定的 Agent ID（必填）")
    metadata: dict[str, Any] | None = Field(None, description="元数据")


class ThreadUpdateRequest(BaseModel):
    """更新线程请求"""

    intent: str | None = Field(None, description="用户意图/标题")
    agent_id: str | None = Field(None, description="绑定的 Agent ID")
    metadata: dict[str, Any] | None = Field(None, description="元数据")


class ThreadResponse(BaseModel):
    """线程响应模型"""

    thread_id: str = Field(..., description="线程 ID")
    current_state: str = Field(..., description="当前状态")
    intent: str = Field(..., description="用户意图/标题")
    created_at: str = Field(..., description="创建时间")
    updated_at: str = Field(..., description="更新时间")
    agent_id: str | None = Field(None, description="绑定的 Agent ID")


class ThreadDetailResponse(ThreadResponse):
    """线程详情响应模型（包含消息）"""

    messages: list["MessageResponse"] = Field(
        default_factory=list, description="消息列表"
    )


class MessageResponse(BaseModel):
    """消息响应模型"""

    id: str = Field(..., description="消息 ID")
    thread_id: str = Field(..., description="线程 ID")
    role: str = Field(..., description="角色：user/assistant/system")
    content: str = Field(..., description="消息内容")
    agent_id: str | None = Field(None, description="Agent ID")
    timestamp: str = Field(..., description="创建时间")
    metadata: dict[str, Any] | None = Field(None, description="额外数据")
