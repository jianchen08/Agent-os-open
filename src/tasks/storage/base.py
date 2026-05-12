"""
任务存储抽象接口

定义任务存储的抽象基类和数据模型，支持多种存储后端的统一接口。
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TaskModel(BaseModel):
    """
    任务数据模型

    用于存储层之间传输的任务数据，独立于数据库模型。
    包含任务的所有核心字段，支持序列化和反序列化。
    """

    # 核心标识
    id: str = Field(..., description="任务ID")

    # 层级关系
    parent_task_id: str | None = Field(None, description="父任务ID")
    execution_record_id: str | None = Field(None, description="关联的执行记录ID")

    # 关联
    user_id: str | None = Field(None, description="所属用户ID")
    session_id: str | None = Field(None, description="来源会话ID")

    # 定义
    title: str = Field(..., description="任务标题")
    description: str | None = Field(None, description="任务描述")
    goal: dict[str, Any] | None = Field(None, description="任务目标")

    # 执行配置
    target_type: str | None = Field(None, description="目标执行者类型")
    target_id: str | None = Field(None, description="目标执行者ID")
    target_name: str | None = Field(None, description="目标执行者名称")
    priority: int = Field(default=5, description="优先级")

    # 依赖关系
    dependencies: list[str] | None = Field(default_factory=list, description="依赖的任务ID列表")
    due_date: datetime | None = Field(None, description="截止日期")
    retry_count: int = Field(default=0, description="重试次数")
    max_retries: int = Field(default=3, description="最大重试次数")

    # 评估指标引用
    evaluation_metric_ids: list[str] | None = Field(
        default_factory=list, description="引用的评估指标ID列表"
    )

    # 状态
    status: str = Field(default="pending", description="任务状态")

    # 时间
    started_at: datetime | None = Field(None, description="开始时间")
    completed_at: datetime | None = Field(None, description="完成时间")
    created_at: datetime | None = Field(None, description="创建时间")
    updated_at: datetime | None = Field(None, description="更新时间")

    # 元数据
    task_metadata: dict[str, Any] | None = Field(None, description="元数据")
    tags: list[str] | None = Field(default_factory=list, description="标签列表")

    model_config = {"from_attributes": True}


class ITaskStorage(ABC):
    """
    任务存储抽象基类

    定义任务存储的统一接口，支持多种存储后端实现。
    所有方法均为异步方法，支持异步 I/O 操作。
    """

    @abstractmethod
    async def save(self, task: TaskModel) -> TaskModel:
        """
        保存任务

        如果任务已存在则更新，否则创建新任务。

        Args:
            task: 任务数据模型

        Returns:
            保存后的任务数据模型

        Raises:
            StorageError: 存储操作失败时抛出
        """

    @abstractmethod
    async def load(self, task_id: str) -> TaskModel | None:
        """
        加载任务

        根据任务ID加载任务数据。

        Args:
            task_id: 任务ID

        Returns:
            任务数据模型，如果不存在则返回 None
        """

    @abstractmethod
    async def load_by_id(self, task_id: str) -> TaskModel | None:
        """
        根据ID加载任务（load 的别名）

        Args:
            task_id: 任务ID

        Returns:
            任务数据模型，如果不存在则返回 None
        """

    @abstractmethod
    async def list_by_status(
        self,
        status: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskModel]:
        """
        按状态列出任务

        Args:
            status: 任务状态
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            任务列表
        """

    @abstractmethod
    async def update_status(
        self,
        task_id: str,
        status: str,
        error_message: str | None = None,
    ) -> bool:
        """
        更新任务状态

        Args:
            task_id: 任务ID
            status: 新状态
            error_message: 错误信息（可选）

        Returns:
            是否更新成功
        """

    @abstractmethod
    async def delete(self, task_id: str) -> bool:
        """
        删除任务

        Args:
            task_id: 任务ID

        Returns:
            是否删除成功
        """

    @abstractmethod
    async def list_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskModel]:
        """
        列出所有任务

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            任务列表
        """


class StorageError(Exception):
    """存储操作异常"""

    def __init__(self, message: str, cause: Exception | None = None):
        """
        初始化存储异常

        Args:
            message: 错误信息
            cause: 原始异常
        """
        super().__init__(message)
        self.cause = cause
