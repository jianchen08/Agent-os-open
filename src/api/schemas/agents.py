"""
Agent 相关数据模型
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AgentCreateRequest(BaseModel):
    """Agent 创建请求"""

    name: str = Field(..., min_length=1, max_length=255, description="Agent 名称")
    model: str = Field(..., description="使用的 LLM 模型")
    system_prompt: str = Field(..., min_length=1, description="系统提示词")
    tool_names: list[str] = Field(default_factory=list, description="可用工具列表")
    max_iterations: int = Field(default=50, gt=0, le=200, description="最大迭代次数")
    timeout: int = Field(default=600, ge=0, le=3600, description="超时时间（秒）")
    description: str | None = Field(None, description="Agent 描述")
    agent_type: str = Field(default="atomic", description="Agent 类型")
    tags: list[str] = Field(default_factory=list, description="标签")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """验证 Agent 名称"""
        if not v or not v.strip():
            raise ValueError("Agent 名称不能为空")
        return v.strip()


class AgentUpdateRequest(BaseModel):
    """Agent 更新请求"""

    name: str | None = Field(
        None, min_length=1, max_length=255, description="Agent 名称"
    )
    model: str | None = Field(None, description="使用的 LLM 模型")
    system_prompt: str | None = Field(None, min_length=1, description="系统提示词")
    tool_names: list[str] | None = Field(None, description="可用工具列表")
    max_iterations: int | None = Field(None, gt=0, le=200, description="最大迭代次数")
    timeout: int | None = Field(None, ge=0, le=3600, description="超时时间（秒）")
    description: str | None = Field(None, description="Agent 描述")
    tags: list[str] | None = Field(None, description="标签")
    metadata: dict[str, Any] | None = Field(None, description="元数据")
    status: str | None = Field(None, description="状态")


class AgentResponse(BaseModel):
    """Agent 响应"""

    id: str = Field(..., description="Agent ID")  # 改为 str 类型，支持自定义 ID 格式
    name: str = Field(..., description="Agent 名称")
    model: str = Field(..., description="使用的 LLM 模型")
    system_prompt: str = Field(..., description="系统提示词")
    tool_names: list[str] = Field(..., description="可用工具列表")
    max_iterations: int = Field(..., description="最大迭代次数")
    timeout: int = Field(..., description="超时时间（秒）")
    description: str | None = Field(None, description="Agent 描述")
    agent_type: str = Field(..., description="Agent 类型")
    status: str = Field(..., description="状态")
    tags: list[str] = Field(default_factory=list, description="标签")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime | None = Field(None, description="更新时间")


class AgentListResponse(BaseModel):
    """Agent 列表响应"""

    items: list[AgentResponse] = Field(..., description="Agent 列表")
    total: int = Field(..., ge=0, description="总数量")
    page: int = Field(..., ge=1, description="当前页码")
    page_size: int = Field(..., ge=1, description="每页数量")
