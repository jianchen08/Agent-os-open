"""
任务客户端模块

基于 src/agents/subagent.py 改造，负责任务提交，不管理资源。
所有资源调度由全局调度器统一处理。
"""

import logging
from dataclasses import dataclass
from typing import Any

from src.core.exceptions import SubAgentNestingError
from src.orchestration.scheduler import get_global_scheduler
from src.orchestration.types import AgentLevel, TargetType, TaskPriority

logger = logging.getLogger(__name__)


@dataclass
class SubAgentConfig:
    """SubAgent 配置类

    用于向后兼容，提供属性访问方式。
    """
    name: str
    description: str
    model_name: str
    system_prompt: str
    tool_ids: list[str]
    max_iterations: int
    timeout_seconds: int


class TaskClient:
    """
    任务客户端

    职责：
    1. 提交 Agent 任务到全局调度器
    2. 提交 Workflow 任务到全局调度器
    3. 等待任务完成并返回结果
    4. 层级控制（检查是否可以创建子任务）

    注意：TaskClient 只负责任务提交，不直接管理资源。
    所有资源调度由全局调度器统一处理。
    """

    def __init__(
        self,
        current_agent_level: AgentLevel,
        session_id: str | None = None,
        parent_record_id: str | None = None,
    ):
        """
        初始化任务客户端

        Args:
            current_agent_level: 当前 Agent 的层级
            session_id: 会话 ID
            parent_record_id: 父执行记录 ID（用于 SubAgent 场景）
        """
        self.current_agent_level = current_agent_level
        self.session_id = session_id
        self.parent_record_id = parent_record_id

        # 确定子 Agent 的层级
        self.target_agent_level = self._get_target_level(current_agent_level)

        # 获取全局调度器
        self.scheduler = get_global_scheduler()

    def _get_target_level(self, current_level: AgentLevel) -> AgentLevel:
        """
        根据当前层级确定目标子 Agent 层级

        Args:
            current_level: 当前 Agent 层级

        Returns:
            目标子 Agent 层级

        Raises:
            SubAgentNestingError: 如果当前层级不能创建子 Agent
        """
        if current_level == AgentLevel.L1:
            return AgentLevel.L2
        elif current_level == AgentLevel.L2:
            return AgentLevel.L3
        else:
            # L3 不能再创建子 Agent
            raise SubAgentNestingError()

    def can_create_subtask(self) -> bool:
        """
        检查当前层级是否可以创建子任务

        Returns:
            是否可以创建子任务
        """
        try:
            self._get_target_level(self.current_agent_level)
            return True
        except SubAgentNestingError:
            return False

    async def submit_agent_task(
        self,
        description: str,
        prompt: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        target_id: str | None = None,
        agent_config: dict[str, Any] | None = None,
        is_subagent_context: bool = False,
        timeout: float | None = None,
    ) -> str:
        """
        提交 Agent 任务并等待完成

        Args:
            description: 任务简短描述
            prompt: 详细执行指令
            priority: 任务优先级
            target_id: 目标 Agent ID
            agent_config: Agent 配置字典
            is_subagent_context: 是否已在 SubAgent 上下文中
            timeout: 超时时间（秒）

        Returns:
            执行结果字符串

        Raises:
            SubAgentNestingError: SubAgent 不能再启动 SubAgent
            asyncio.TimeoutError: 执行超时
        """
        logger.info(
            f"[TaskClient] 提交 Agent 任务 | "
            f"current_level={self.current_agent_level.name} | "
            f"target_level={self.target_agent_level.name} | "
            f"target_id={target_id} | "
            f"description={description[:50]}... | "
            f"priority={priority.name}"
        )

        # 检查嵌套
        if is_subagent_context:
            logger.warning(
                "[TaskClient] 嵌套检查失败 | "
                "已在 SubAgent 上下文中，无法创建新的 SubAgent"
            )
            raise SubAgentNestingError()

        try:
            # 提交任务到全局调度器，传递 parent_record_id 用于执行记录父子关系
            task_id = await self.scheduler.submit_task(
                agent_level=self.target_agent_level,
                description=description,
                prompt=prompt,
                priority=priority,
                target_type=TargetType.AGENT,
                parent_record_id=self.parent_record_id,
                session_id=self.session_id,
                config={
                    "target_id": target_id,
                    "agent_config": agent_config or {},
                },
            )

            logger.info(
                f"[TaskClient] Agent 任务已提交 | "
                f"task_id={task_id} | "
                f"level={self.target_agent_level.name} | "
                f"priority={priority.name}"
            )

            # 等待任务完成
            completed_task = await self.scheduler.wait_for_completion(
                task_id=task_id, timeout=timeout
            )

            # 返回执行结果
            if completed_task.status.value == "completed":
                result = completed_task.result
                if result and result.get("success"):
                    logger.info(f"[TaskClient] Agent 任务执行成功 | task_id={task_id}")
                    return result.get("output", "任务完成")
                else:
                    logger.warning(
                        f"[TaskClient] Agent 任务执行失败 | "
                        f"task_id={task_id} | "
                        f"error={result.get('error', '未知错误')}"
                    )
                    return f"Agent 执行失败: {result.get('error', '未知错误')}"
            else:
                logger.error(
                    f"[TaskClient] Agent 任务未正常完成 | "
                    f"task_id={task_id} | "
                    f"status={completed_task.status.value} | "
                    f"error={completed_task.error}"
                )
                return f"Agent 执行失败: {completed_task.error or '任务未正常完成'}"

        except Exception as e:
            logger.error(
                f"[TaskClient] Agent 任务执行异常 | "
                f"description={description[:50]}... | "
                f"error={str(e)}",
                exc_info=True,
            )
            return f"Agent 执行异常: {str(e)}"

    async def submit_workflow_task(
        self,
        description: str,
        workflow: Any,
        inputs: dict[str, Any] | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        is_subagent_context: bool = False,
        timeout: float | None = None,
    ) -> str:
        """
        提交 Workflow 任务并等待完成

        Args:
            description: 任务简短描述
            workflow: 工作流定义对象（UWF 格式）
            inputs: 工作流输入参数
            priority: 任务优先级
            is_subagent_context: 是否已在 SubAgent 上下文中
            timeout: 超时时间（秒）

        Returns:
            执行结果字符串

        Raises:
            SubAgentNestingError: SubAgent 不能再启动工作流
            asyncio.TimeoutError: 执行超时
        """
        logger.info(
            f"[TaskClient] 提交 Workflow 任务 | "
            f"current_level={self.current_agent_level.name} | "
            f"target_level={self.target_agent_level.name} | "
            f"description={description[:50]}... | "
            f"priority={priority.name}"
        )

        # 检查嵌套
        if is_subagent_context:
            logger.warning(
                "[TaskClient] 嵌套检查失败 | "
                "已在 SubAgent 上下文中，无法启动工作流"
            )
            raise SubAgentNestingError()

        try:
            # 提交任务到全局调度器，传递 parent_record_id 用于执行记录父子关系
            task_id = await self.scheduler.submit_task(
                agent_level=self.target_agent_level,
                description=description,
                prompt="",  # 工作流不需要 prompt
                priority=priority,
                target_type=TargetType.WORKFLOW,
                parent_record_id=self.parent_record_id,
                session_id=self.session_id,
                config={
                    "workflow": workflow,
                    "inputs": inputs or {},
                },
            )

            logger.info(
                f"[TaskClient] Workflow 任务已提交 | "
                f"task_id={task_id} | "
                f"level={self.target_agent_level.name} | "
                f"priority={priority.name}"
            )

            # 等待任务完成
            completed_task = await self.scheduler.wait_for_completion(
                task_id=task_id, timeout=timeout
            )

            # 返回执行结果
            if completed_task.status.value == "completed":
                result = completed_task.result
                if result and result.get("success"):
                    logger.info(f"[TaskClient] Workflow 任务执行成功 | task_id={task_id}")
                    outputs = result.get("outputs", {})
                    return str(outputs) if outputs else "工作流执行完成"
                else:
                    logger.warning(
                        f"[TaskClient] Workflow 任务执行失败 | "
                        f"task_id={task_id} | "
                        f"error={result.get('error', '未知错误')}"
                    )
                    return f"工作流执行失败: {result.get('error', '未知错误')}"
            else:
                logger.error(
                    f"[TaskClient] Workflow 任务未正常完成 | "
                    f"task_id={task_id} | "
                    f"status={completed_task.status.value} | "
                    f"error={completed_task.error}"
                )
                return f"工作流执行失败: {completed_task.error or '任务未正常完成'}"

        except Exception as e:
            logger.error(
                f"[TaskClient] Workflow 任务执行异常 | "
                f"description={description[:50]}... | "
                f"error={str(e)}",
                exc_info=True,
            )
            return f"工作流执行异常: {str(e)}"

    async def submit_task_async(
        self,
        description: str,
        prompt: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        target_type: TargetType = TargetType.AGENT,
        target_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> str:
        """
        异步提交任务（不等待完成）

        Args:
            description: 任务描述
            prompt: 任务提示
            priority: 任务优先级
            target_type: 目标类型（agent/workflow/tool）
            target_id: 目标 ID
            config: 额外配置

        Returns:
            任务 ID
        """
        task_id = await self.scheduler.submit_task(
            agent_level=self.target_agent_level,
            description=description,
            prompt=prompt,
            priority=priority,
            target_type=target_type,
            parent_record_id=self.parent_record_id,
            session_id=self.session_id,
            config={
                "target_id": target_id,
                **(config or {}),
            },
        )

        logger.info(
            f"[TaskClient] 任务已异步提交 | "
            f"task_id={task_id} | "
            f"target_type={target_type.value} | "
            f"target_id={target_id}"
        )
        return task_id

    async def get_task_status(self, task_id: str):
        """
        获取任务状态

        Args:
            task_id: 任务 ID

        Returns:
            任务状态
        """
        return await self.scheduler.get_task_status(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        """
        取消任务

        Args:
            task_id: 任务 ID

        Returns:
            是否成功取消
        """
        return await self.scheduler.cancel_task(task_id)

    def get_scheduler_stats(self) -> dict:
        """
        获取调度器统计信息

        Returns:
            统计信息
        """
        return self.scheduler.get_statistics()


class TaskClientFactory:
    """
    任务客户端工厂

    用于创建不同层级的 TaskClient 实例。
    """

    @staticmethod
    def create(
        agent_level: AgentLevel,
        session_id: str | None = None,
    ) -> TaskClient:
        """
        创建任务客户端

        Args:
            agent_level: Agent 层级
            session_id: 会话 ID

        Returns:
            TaskClient 实例
        """
        return TaskClient(
            current_agent_level=agent_level,
            session_id=session_id,
        )

    @staticmethod
    def create_for_l1(session_id: str | None = None) -> TaskClient:
        """
        创建 L1 层级的任务客户端

        Args:
            session_id: 会话 ID

        Returns:
            TaskClient 实例
        """
        return TaskClient(
            current_agent_level=AgentLevel.L1,
            session_id=session_id,
        )

    @staticmethod
    def create_for_l2(session_id: str | None = None) -> TaskClient:
        """
        创建 L2 层级的任务客户端

        Args:
            session_id: 会话 ID

        Returns:
            TaskClient 实例
        """
        return TaskClient(
            current_agent_level=AgentLevel.L2,
            session_id=session_id,
        )


# === 向后兼容：保留 SubAgentManager 别名 ===

class SubAgentManager(TaskClient):
    """
    SubAgent 管理器（向后兼容）

    继承自 TaskClient，提供与旧版 SubAgentManager 相同的接口。
    """

    # 禁止的工具列表（向后兼容）
    FORBIDDEN_TOOLS = ["Task"]

    def __init__(
        self,
        tool_registry=None,
        tool_executor=None,
        current_agent_level: AgentLevel = AgentLevel.L1,
        session_id: str | None = None,
    ):
        """
        初始化 SubAgent 管理器

        Args:
            tool_registry: 工具注册表（已废弃，保留参数用于兼容）
            tool_executor: 工具执行器（已废弃，保留参数用于兼容）
            current_agent_level: 当前 Agent 的层级
            session_id: 会话 ID
        """
        super().__init__(
            current_agent_level=current_agent_level,
            session_id=session_id,
        )
        # 保存参数用于兼容，但不再使用
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor

    async def create_and_run(
        self,
        description: str,
        prompt: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        target_id: str | None = None,
        target_type: str = "agent",
        is_subagent_context: bool = False,
        timeout: float | None = None,
    ) -> str:
        """
        创建并运行 SubAgent（向后兼容）

        Args:
            description: 任务简短描述
            prompt: 详细执行指令
            priority: 任务优先级
            target_id: 目标 Agent 或工作流 ID
            target_type: 目标类型（agent 或 workflow）
            is_subagent_context: 是否已在 SubAgent 上下文中
            timeout: 超时时间（秒）

        Returns:
            执行结果字符串
        """
        if target_type == "workflow":
            # 工作流类型需要 workflow 对象，这里简化处理
            logger.warning(
                "[SubAgentManager] workflow 类型需要 workflow 对象，"
                "请使用 submit_workflow_task 方法"
            )
            return "错误：workflow 类型需要 workflow 对象"

        return await self.submit_agent_task(
            description=description,
            prompt=prompt,
            priority=priority,
            target_id=target_id,
            is_subagent_context=is_subagent_context,
            timeout=timeout,
        )

    @classmethod
    def get_default_subagent_config(
        cls,
        description: str,
        parent_model: str | None = None,
    ) -> "SubAgentConfig":
        """
        获取默认的 SubAgent 配置（向后兼容）

        Args:
            description: 任务描述
            parent_model: 父 Agent 使用的模型

        Returns:
            SubAgentConfig 配置对象
        """
        import uuid

        # 生成唯一的 agent 名称
        unique_name = f"subagent_{uuid.uuid4().hex[:8]}"

        return SubAgentConfig(
            name=unique_name,
            description=description,
            model_name=parent_model or "gpt-4",
            system_prompt=f"你是一个SubAgent，负责执行特定任务: {description}",
            tool_ids=["file_operate", "search", "enhanced_search"],
            max_iterations=30,
            timeout_seconds=300,
        )


class SubAgentManagerFactory:
    """SubAgentManager 工厂类（向后兼容）

    提供与旧版 SubAgentManagerFactory 相同的接口。
    """

    def __init__(
        self,
        tool_registry=None,
        tool_executor=None,
        current_agent_level: AgentLevel = AgentLevel.L1,
        session_id: str | None = None,
    ):
        """
        初始化工厂

        Args:
            tool_registry: 工具注册表（已废弃，保留参数用于兼容）
            tool_executor: 工具执行器（已废弃，保留参数用于兼容）
            current_agent_level: 当前 Agent 的层级
            session_id: 会话 ID
        """
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._current_agent_level = current_agent_level
        self._session_id = session_id

    def create_manager(self) -> SubAgentManager:
        """
        创建 SubAgentManager 实例

        Returns:
            SubAgentManager 实例
        """
        return SubAgentManager(
            tool_registry=self._tool_registry,
            tool_executor=self._tool_executor,
            current_agent_level=self._current_agent_level,
            session_id=self._session_id,
        )
