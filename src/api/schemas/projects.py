"""
长期任务（项目）相关 Schema（基于 Task 模型）

注意：没有独立的 Project 数据模型，长期任务通过 Task 表的 parent_task_id=None 实现
"""

from typing import Any

from pydantic import BaseModel, Field


class ProjectCreateRequest(BaseModel):
    """创建长期任务请求"""

    goal: str = Field(..., description="长期目标描述", min_length=1)
    session_id: str = Field(..., description="关联会话 ID")
    evaluation_metric_ids: list[str] | None = Field(
        default_factory=list, description="评估指标 ID 列表"
    )
    auto_execute: bool = Field(False, description="是否自动执行")
    metadata: dict[str, Any] | None = Field(None, description="元数据")


class ProjectResponse(BaseModel):
    """长期任务响应"""

    project_id: str = Field(..., description="项目 ID")
    user_id: str = Field(..., description="用户 ID")
    session_id: str = Field(..., description="会话 ID")
    goal: str = Field(..., description="长期目标")
    status: str = Field(..., description="状态")
    auto_execute: bool = Field(..., description="是否自动执行")
    current_task_index: int = Field(0, description="当前任务索引")
    tasks: list[dict[str, Any]] = Field(default_factory=list, description="子任务列表")
    total_tasks: int = Field(0, description="总任务数")
    completed_tasks: int = Field(0, description="已完成任务数")
    created_at: str = Field(..., description="创建时间")
    updated_at: str | None = Field(None, description="更新时间")


class ProjectListResponse(BaseModel):
    """长期任务列表响应"""

    items: list[dict[str, Any]] = Field(..., description="项目列表")
    total: int = Field(..., description="总数")
    limit: int = Field(..., description="每页数量")
    offset: int = Field(..., description="偏移量")


class ProjectAutoExecuteRequest(BaseModel):
    """切换自动执行请求"""

    enabled: bool = Field(..., description="是否启用")


class ProjectAutoExecuteResponse(BaseModel):
    """切换自动执行响应"""

    project_id: str = Field(..., description="项目 ID")
    auto_execute: bool = Field(..., description="是否自动执行")
    toggled_at: str = Field(..., description="切换时间")


class ProjectControlResponse(BaseModel):
    """项目控制响应"""

    project_id: str = Field(..., description="项目 ID")
    status: str = Field(..., description="状态")
    controlled_at: str = Field(..., description="操作时间")
