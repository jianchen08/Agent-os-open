"""
工具相关数据模型
"""

from typing import Any

from pydantic import BaseModel, Field


class ToolResponse(BaseModel):
    """工具响应"""

    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    input_schema: dict[str, Any] = Field(..., description="输入参数 Schema")
    output_schema: dict[str, Any] | None = Field(None, description="输出 Schema")
    source: str = Field(..., description="工具来源")
    category: str | None = Field(None, description="工具分类")
    requires_approval: bool = Field(False, description="是否需要审批")
    version: str = Field(default="1.0.0", description="版本号")
    tags: list[str] = Field(default_factory=list, description="标签")
    status: str = Field(..., description="工具状态")


class ToolListResponse(BaseModel):
    """工具列表响应"""

    items: list[ToolResponse] = Field(..., description="工具列表")
    total: int = Field(..., ge=0, description="总数量")
    page: int = Field(..., ge=1, description="当前页码")
    page_size: int = Field(..., ge=1, description="每页数量")
