"""
通用数据模型
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class MessageResponse(BaseModel):
    """消息响应"""

    message: str = Field(..., description="消息内容")
    success: bool = Field(default=True, description="是否成功")


class PaginatedResponse(BaseModel, Generic[T]):
    """分页响应基类"""

    items: list[T] = Field(..., description="数据列表")
    total: int = Field(..., ge=0, description="总数量")
    page: int = Field(..., ge=1, description="当前页码")
    page_size: int = Field(..., ge=1, description="每页数量")

    @property
    def total_pages(self) -> int:
        """计算总页数"""
        if self.page_size == 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size

    @property
    def has_next(self) -> bool:
        """是否有下一页"""
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        """是否有上一页"""
        return self.page > 1
