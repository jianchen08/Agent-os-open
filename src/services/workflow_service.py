"""
工作流服务

提供工作流的增删改查功能
"""

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundException, ValidationException
from src.db.models import Workflow
from src.workflows.loader import WorkflowLoader
from src.workflows.types import UWF

logger = logging.getLogger(__name__)


class WorkflowService:
    """工作流服务"""

    def __init__(self, db: AsyncSession):
        """
        初始化工作流服务

        Args:
            db: 数据库会话
        """
        self.db = db

    async def list_workflows(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """
        获取工作流列表

        Args:
            user_id: 用户ID
            page: 页码
            page_size: 每页数量
            status: 状态过滤
            search: 搜索关键词

        Returns:
            工作流列表响应
        """
        # 构建查询
        query = select(Workflow)

        # 状态过滤
        if status:
            query = query.where(Workflow.status == status)

        # 搜索过滤
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (Workflow.name.ilike(search_pattern))
                | (Workflow.description.ilike(search_pattern))
            )

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # 分页查询
        query = query.offset((page - 1) * page_size).limit(page_size)
        query = query.order_by(Workflow.created_at.desc())

        result = await self.db.execute(query)
        workflows = result.scalars().all()

        # 转换为响应格式
        items = [
            {
                "id": str(workflow.id),
                "name": workflow.name,
                "description": workflow.description,
                "definition": workflow.definition,
                "status": workflow.status,
                "tags": workflow.tags or [],
                "created_at": workflow.created_at.isoformat(),
                "updated_at": (
                    workflow.updated_at.isoformat() if workflow.updated_at else None
                ),
            }
            for workflow in workflows
        ]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def get_workflow(self, workflow_id: UUID, user_id: str) -> dict[str, Any]:
        """
        获取工作流详情

        Args:
            workflow_id: 工作流ID
            user_id: 用户ID

        Returns:
            工作流详情

        Raises:
            NotFoundException: 工作流不存在
        """
        query = select(Workflow).where(Workflow.id == str(workflow_id))
        result = await self.db.execute(query)
        workflow = result.scalar_one_or_none()

        if workflow is None:
            raise NotFoundException(
                message=f"工作流不存在: {workflow_id}",
                resource_type="Workflow",
                resource_id=str(workflow_id),
                code="WORKFLOW_001",
            )

        return {
            "id": str(workflow.id),
            "name": workflow.name,
            "description": workflow.description,
            "definition": workflow.definition,
            "status": workflow.status,
            "tags": workflow.tags or [],
            "created_at": workflow.created_at.isoformat(),
            "updated_at": (
                workflow.updated_at.isoformat() if workflow.updated_at else None
            ),
        }

    async def create_workflow(
        self,
        user_id: str,
        name: str,
        definition: dict[str, Any],
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        创建工作流

        Args:
            user_id: 用户ID
            name: 工作流名称
            definition: 工作流定义
            description: 工作流描述
            tags: 标签

        Returns:
            创建的工作流
        """
        workflow = Workflow(
            name=name,
            description=description,
            definition=definition,
            status="draft",
            tags=tags or [],
            created_by=user_id,
        )

        self.db.add(workflow)
        await self.db.commit()
        await self.db.refresh(workflow)

        return {
            "id": str(workflow.id),
            "name": workflow.name,
            "description": workflow.description,
            "definition": workflow.definition,
            "status": workflow.status,
            "tags": tags or [],
            "created_at": workflow.created_at.isoformat(),
            "updated_at": (
                workflow.updated_at.isoformat() if workflow.updated_at else None
            ),
        }

    async def update_workflow(
        self, workflow_id: UUID, user_id: str, **kwargs
    ) -> dict[str, Any]:
        """
        更新工作流

        Args:
            workflow_id: 工作流ID
            user_id: 用户ID
            **kwargs: 更新字段

        Returns:
            更新后的工作流

        Raises:
            NotFoundException: 工作流不存在
            ValidationException: 状态值无效
        """
        # 验证状态值
        if "status" in kwargs:
            valid_statuses = ["draft", "active", "archived"]
            if kwargs["status"] not in valid_statuses:
                raise ValidationException(
                    message=f"无效的状态值: {kwargs['status']}",
                    field="status",
                    details={"valid_statuses": valid_statuses},
                    code="WORKFLOW_008",
                )

        # 构建更新数据
        update_data = {}
        if "name" in kwargs:
            update_data["name"] = kwargs["name"]
        if "description" in kwargs:
            update_data["description"] = kwargs["description"]
        if "definition" in kwargs:
            update_data["definition"] = kwargs["definition"]
        if "status" in kwargs:
            update_data["status"] = kwargs["status"]

        if not update_data:
            # 没有需要更新的字段，返回现有数据
            return await self.get_workflow(workflow_id, user_id)

        # 检查工作流是否存在
        check_query = select(Workflow).where(Workflow.id == str(workflow_id))
        check_result = await self.db.execute(check_query)
        if check_result.scalar_one_or_none() is None:
            raise NotFoundException(
                message=f"工作流不存在: {workflow_id}",
                resource_type="Workflow",
                resource_id=str(workflow_id),
                code="WORKFLOW_001",
            )

        # 执行更新
        query = (
            update(Workflow)
            .where(Workflow.id == str(workflow_id))
            .values(**update_data)
        )
        await self.db.execute(query)
        await self.db.commit()

        return await self.get_workflow(workflow_id, user_id)

    async def delete_workflow(self, workflow_id: UUID, user_id: str) -> None:
        """
        删除工作流

        Args:
            workflow_id: 工作流ID
            user_id: 用户ID

        Raises:
            NotFoundException: 工作流不存在
        """
        # 检查工作流是否存在
        check_query = select(Workflow).where(Workflow.id == str(workflow_id))
        check_result = await self.db.execute(check_query)
        if check_result.scalar_one_or_none() is None:
            raise NotFoundException(
                message=f"工作流不存在: {workflow_id}",
                resource_type="Workflow",
                resource_id=str(workflow_id),
                code="WORKFLOW_001",
            )

        query = delete(Workflow).where(Workflow.id == str(workflow_id))
        await self.db.execute(query)
        await self.db.commit()

    async def execute_workflow(
        self,
        workflow_id: str,
        inputs: dict[str, Any],
        timeout: int = 1800,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        执行工作流

        Args:
            workflow_id: 工作流 ID（如 "resource_generation"）
            inputs: 工作流输入参数
            timeout: 超时时间（秒），默认 1800 秒（30 分钟）
            config: 执行配置（可选）

        Returns:
            执行结果字典，包含：
            - execution_id: 执行 ID
            - status: 状态 (running, completed, failed)
            - output: 输出结果（如果完成）
            - error: 错误信息（如果失败）
        """
        logger.info(
            f"[WorkflowService.execute_workflow] 开始执行 | "
            f"workflow_id={workflow_id} | timeout={timeout}"
        )

        try:
            # 1. 尝试从数据库加载工作流
            workflow = None
            try:
                workflow = await self._load_workflow_by_id(workflow_id)
            except Exception as e:
                logger.debug(f"[WorkflowService] 数据库加载失败: {e}，尝试从文件加载")

            # 2. 如果数据库加载失败，尝试从 YAML 文件加载
            if workflow is None:
                workflow = await self._load_workflow_from_file(workflow_id)

            if not workflow:
                return {
                    "status": "failed",
                    "error": f"工作流不存在: {workflow_id}",
                    "execution_id": None,
                }

            # 3. 创建工作流执行器（使用 LangGraphWorkflowExecutor）
            from src.core.di import get_global_container
            from src.tools.executor import ToolExecutor
            from src.tools.global_registry import get_global_tool_registry
            from src.workflows.langgraph_executor import LangGraphWorkflowExecutor

            # 获取全局工具注册表
            tool_registry = await get_global_tool_registry(session=self.db)

            # 创建工具执行器
            tool_executor = ToolExecutor(registry=tool_registry)

            # 获取 LLM 客户端
            container = get_global_container()
            llm_factory = container.get("llm_factory")
            llm_client = llm_factory.get_default_client()

            executor = LangGraphWorkflowExecutor(
                event_bus=None,
                tool_executor=tool_executor,
                llm_client=llm_client,
                session=self.db,
            )

            # 4. 执行工作流
            result = await executor.execute(
                workflow=workflow,
                inputs=inputs,
                config=config or {"timeout": timeout},
            )

            return result

        except Exception as e:
            logger.exception(f"[WorkflowService.execute_workflow] 执行异常 | error={e}")
            return {
                "status": "failed",
                "error": str(e),
                "execution_id": None,
            }

    async def _load_workflow_by_id(self, workflow_id: str) -> UWF | None:
        """从数据库加载工作流"""
        import json

        from src.db.models import Workflow

        # 首先尝试按 name 查询
        query = select(Workflow).where(Workflow.name == workflow_id)
        result = await self.db.execute(query)
        workflow_record = result.scalar_one_or_none()

        # 如果没找到，尝试按 definition.id 查询（使用 JSON 函数）
        if not workflow_record:
            query = select(Workflow).where(
                Workflow.definition["id"].astext == workflow_id
            )
            result = await self.db.execute(query)
            workflow_record = result.scalar_one_or_none()

        if not workflow_record:
            return None

        # 转换为 UWF 格式
        from src.workflows.loader import WorkflowLoader

        WorkflowLoader(session=self.db)

        definition = workflow_record.definition
        if isinstance(definition, str):
            definition = json.loads(definition)

        # 创建 UWF 对象
        from src.workflows.types import UWF

        return UWF(
            id=definition.get("id", workflow_id),
            version=definition.get("version", "1.0"),
            metadata=definition.get("metadata", {}),
            inputs=definition.get("inputs", {}),
            outputs=definition.get("outputs", {}),
            nodes=definition.get("nodes", []),
            edges=definition.get("edges", []),
            execution=definition.get("execution", {}),
            triggers=definition.get("triggers", []),
            state=definition.get("state", {}),
            status=definition.get("status", "active"),
        )

    async def _load_workflow_from_file(self, workflow_id: str) -> UWF | None:
        """从 YAML 文件加载工作流"""
        from src.config.settings import get_settings

        settings = get_settings()
        config_dir = (
            Path(settings.config_dir)
            if hasattr(settings, "config_dir")
            else Path("config")
        )

        # 尝试从 config/workflows/resource 目录加载
        workflow_file = config_dir / "workflows" / "resource" / f"{workflow_id}.yaml"

        if not workflow_file.exists():
            # 尝试从 config/workflows 目录加载
            workflow_file = config_dir / "workflows" / f"{workflow_id}.yaml"

        if not workflow_file.exists():
            return None

        loader = WorkflowLoader(session=self.db)
        workflow = await loader.load_from_yaml(workflow_file)

        logger.info(f"[WorkflowService] 从文件加载工作流 | file={workflow_file}")

        return workflow
