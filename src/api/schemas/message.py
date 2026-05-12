"""
消息相关的数据模型和 Schema 定义
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    """消息响应模型"""

    id: UUID = Field(..., description="消息 ID")
    session_id: UUID = Field(..., description="会话 ID")
    parent_id: UUID | None = Field(None, description="父消息 ID")
    sequence: int = Field(..., description="消息序号")
    role: str = Field(..., description="角色：user/assistant/system")
    agent_name: str | None = Field(None, description="Agent 名称")
    content: str | None = Field(None, description="消息内容")
    tool_calls: dict[str, Any] | None = Field(None, description="工具调用信息")
    tool_call_id: str | None = Field(None, description="工具调用 ID")
    extra_data: dict[str, Any] | None = Field(None, description="额外数据")
    created_at: datetime = Field(..., description="创建时间")

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls, obj) -> "MessageResponse":
        """从 ORM 模型转换"""
        return cls(
            id=obj.id,
            session_id=obj.session_id,
            parent_id=obj.parent_id,
            sequence=obj.sequence,
            role=obj.role,
            agent_name=obj.agent_name,
            content=obj.content,
            tool_calls=obj.tool_calls,
            tool_call_id=obj.tool_call_id,
            extra_data=obj.extra_data,
            created_at=obj.created_at,
        )


class MessageEditRequest(BaseModel):
    """消息编辑请求"""

    content: str = Field(..., min_length=1, description="新的消息内容")


class MessageRetryRequest(BaseModel):
    """消息重试请求"""

    new_content: str | None = Field(None, description="新的消息内容（可选）")
    regenerate_all: bool = Field(default=False, description="是否重新生成所有后续消息")


class MessageListResponse(BaseModel):
    """消息列表响应"""

    messages: list[MessageResponse] = Field(..., description="消息列表")
    total: int = Field(..., description="总数量")
    session_id: str = Field(..., description="会话 ID")
