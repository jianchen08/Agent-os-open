"""
任务提交编排器

负责协调任务提交的完整业务流程，包括：
1. Agent 验证
2. 评估指标解析
3. 层级权限验证
4. 任务提交
5. Agent 调用记录

将业务逻辑从 TaskSubmitTool 中解耦，使工具层保持简单。

事件驱动改造：
- 任务提交后发布 task.submitted 事件
- 由 TaskSchedulerService 订阅事件并触发执行
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.level_controller import LevelController
from src.core.results import ToolExecutionResult
from src.db.models import AgentConfig
from src.evaluation.metric_loader import get_metric_loader
from src.tasks.services.submission_service import TaskSubmissionService
from src.tools.types import (
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class TaskSubmitOrchestrator:
    """
    任务提交编排器

    负责协调任务提交的完整业务流程，将业务逻辑从工具层解耦。

    核心职责：
    1. Agent 存在性和状态验证
    2. 评估指标解析和验证
    3. 层级权限验证
    4. 任务提交协调
    5. Agent 调用记录管理

    事件驱动改造：
    - 任务提交后发布 task.submitted 事件
    - 由 TaskSchedulerService 订阅事件并触发执行

    Example:
        >>> orchestrator = TaskSubmitOrchestrator(
        ...     session=session,
        ...     submission_service=TaskSubmissionService(session),
        ...     level_controller=AgentLevelController(),
        ... )
        >>> result = await orchestrator.submit_to_agent(
        ...     goal={"title": "实现登录功能"},
        ...     acceptance_criteria={"file_check": {...}},
        ...     target_id="lingxi",
        ... )
    """

    def __init__(
        self,
        session: AsyncSession,
        submission_service: TaskSubmissionService,
        level_controller: LevelController,
    ):
        """
        初始化任务提交编排器

        Args:
            session: 数据库会话
            submission_service: 任务提交服务
            level_controller: Agent 层级控制器

        Note:
            任务执行通过事件驱动，不再需要 executor_callback。
        """
        self.session = session
        self.submission_service = submission_service
        self.level_controller = level_controller
        self.metric_loader = get_metric_loader()

    async def submit_to_agent(
        self,
        goal: dict[str, Any],
        acceptance_criteria: dict[str, Any],
        target_id: str,
        parent_agent_level: int = 1,
        **kwargs,
    ) -> ToolExecutionResult:
        """
        提交任务给 Agent 执行

        包含完整的业务流程：验证 Agent、解析指标、验证层级、提交任务、触发回调。

        Args:
            goal: 任务目标，必须包含 title 字段
            acceptance_criteria: 验收标准字典
            target_id: 目标 Agent ID 或名称
            parent_agent_level: 父 Agent 层级（1 或 2）
            **kwargs: 其他参数，包括：
                - user_id: 用户 ID
                - parent_task_id: 父任务 ID
                - session_id: 会话 ID
                - task_scope: 任务范围
                - priority: 优先级
                - max_retries: 最大重试次数
                - metadata: 元数据
                - dependencies: 依赖任务 ID 列表
                - tool_record_id: 工具执行记录 ID

        Returns:
            ToolExecutionResult: 成功或失败结果
        """
        logger.info(
            f"[TaskSubmitOrchestrator.submit_to_agent] 开始 | "
            f"target_id={target_id} | parent_agent_level=L{parent_agent_level}"
        )

        try:
            # 1. 验证 Agent 存在
            agent = await self._validate_agent(target_id, parent_agent_level)
            if isinstance(agent, ToolExecutionResult):
                return agent

            # 2. 解析验收标准
            resolved_criteria = await self._resolve_acceptance_criteria(acceptance_criteria)
            logger.info(
                f"[TaskSubmitOrchestrator] 解析后的验收标准 | "
                f"metrics={list(resolved_criteria.keys())}"
            )

            # 3. 记录 Agent 调用
            execution_id = await self._record_agent_call(
                target_id=target_id,
                agent=agent,
                goal=goal,
                parent_agent_level=parent_agent_level,
                kwargs=kwargs,
            )

            # 4. 提交任务
            submit_result = await self._submit_task(
                goal=goal,
                acceptance_criteria=resolved_criteria,
                target_type="agent",
                target_id=target_id,
                target_name=agent.name,
                kwargs=kwargs,
                execution_id=execution_id,
            )

            if submit_result.get("error"):
                return create_failure_result(
                    error=submit_result["error"],
                    error_code=submit_result.get("error_code", "SUBMIT_FAILED"),
                )

            task_id = submit_result["task_id"]

            # 5. 更新 Agent 调用记录（必须在 commit 前，确保数据持久化）
            await self._complete_agent_call(
                execution_id=execution_id,
                task_id=task_id,
            )

            # 6. 提交事务（所有准备工作完成后才持久化）
            await self.session.commit()

            # 7. 构建返回数据
            result = self._build_agent_result(
                submit_result=submit_result,
                agent=agent,
            )

            # BUG-FIX-fix_20260516_double_dispatch:
            # 移除直接 schedule_task 调用。任务执行已由 SubmissionService.publish_submitted_event
            # → EventBus → TaskWorker._on_task_submitted 事件驱动路径完成调度，
            # 此处直接调用 schedule_task 会导致同一任务被调度两次（双重调度竞态条件）。
            logger.info(
                f"[TaskSubmitOrchestrator] 任务已提交 | task_id={task_id}（调度由事件驱动）"
            )

            return result

        except Exception as e:
            logger.error(
                f"[TaskSubmitOrchestrator.submit_to_agent] 失败 | error={str(e)}",
                exc_info=True,
            )
            return create_failure_result(
                error=f"提交失败: {str(e)}",
                error_code="SUBMIT_FAILED",
            )

    async def submit_to_workflow(
        self,
        goal: dict[str, Any],
        acceptance_criteria: dict[str, Any],
        workflow_id: str,
        **kwargs,
    ) -> ToolExecutionResult:
        """
        提交任务给工作流执行

        Args:
            goal: 任务目标
            acceptance_criteria: 验收标准
            workflow_id: 工作流 ID
            **kwargs: 其他参数

        Returns:
            ToolExecutionResult: 成功或失败结果
        """
        logger.info(
            f"[TaskSubmitOrchestrator.submit_to_workflow] 开始 | workflow_id={workflow_id}"
        )

        try:
            # 1. 解析验收标准
            resolved_criteria = await self._resolve_acceptance_criteria(acceptance_criteria)

            # 2. 生成执行记录 ID
            execution_record_id = await self._generate_workflow_execution_id(kwargs)

            # 3. 提交任务
            submit_result = await self._submit_task(
                goal=goal,
                acceptance_criteria=resolved_criteria,
                target_type="workflow",
                target_id=workflow_id,
                target_name=None,
                kwargs={**kwargs, "execution_record_id": execution_record_id},
            )

            if submit_result.get("error"):
                return create_failure_result(
                    error=submit_result["error"],
                    error_code=submit_result.get("error_code", "SUBMIT_FAILED"),
                )

            task_id = submit_result["task_id"]

            # 4. 提交事务（所有准备工作完成后才持久化）
            await self.session.commit()

            # 5. 构建返回数据
            result = self._build_workflow_result(
                submit_result=submit_result,
            )

            # BUG-FIX-fix_20260516_double_dispatch:
            # 移除直接 schedule_task 调用。任务执行已由 SubmissionService.publish_submitted_event
            # → EventBus → TaskWorker._on_task_submitted 事件驱动路径完成调度，
            # 此处直接调用 schedule_task 会导致同一任务被调度两次（双重调度竞态条件）。
            logger.info(
                f"[TaskSubmitOrchestrator] 工作流任务已提交 | task_id={task_id}（调度由事件驱动）"
            )

            return result

        except Exception as e:
            logger.error(
                f"[TaskSubmitOrchestrator.submit_to_workflow] 失败 | error={str(e)}",
                exc_info=True,
            )
            return create_failure_result(
                error=f"提交失败: {str(e)}",
                error_code="SUBMIT_FAILED",
            )

    async def _validate_agent(self, target_id: str, parent_agent_level: int = 1) -> AgentConfig | ToolExecutionResult:
        """
        验证 Agent 存在性和状态

        Args:
            target_id: Agent ID 或名称
            parent_agent_level: 调用者层级（1 或 2）

        Returns:
            AgentConfig 或 ToolExecutionResult（失败时）
        """
        # 先按 config_id 查找（最常用）
        result = await self.session.execute(
            select(AgentConfig).where(AgentConfig.config_id == target_id).limit(1)
        )
        agent = result.scalar_one_or_none()

        # 再按 name 查找
        if not agent:
            result = await self.session.execute(
                select(AgentConfig).where(AgentConfig.name == target_id).limit(1)
            )
            agent = result.scalar_one_or_none()

        # 最后按 ID 查找（兼容 UUID）
        if not agent:
            result = await self.session.execute(
                select(AgentConfig).where(AgentConfig.id == target_id).limit(1)
            )
            agent = result.scalar_one_or_none()

        if not agent:
            available_agents = await self._get_available_agents(parent_agent_level)
            agents_list = "\n".join(
                [f"  - {a['config_id']} (L{a['level']} - {a['name']})" for a in available_agents[:5]]
            )
            error_msg = (
                f"Agent '{target_id}' 不存在。\n\n"
                f"【可用的 Agent】\n{agents_list}"
            )
            return create_failure_result(error=error_msg, error_code="AGENT_NOT_FOUND")

        if not agent.is_active:
            return create_failure_result(
                error=f"Agent 未激活: {target_id}",
                error_code="AGENT_INACTIVE",
            )

        logger.info(
            f"[TaskSubmitOrchestrator._validate_agent] Agent 验证通过 | name={agent.name}"
        )
        return agent

    async def _resolve_acceptance_criteria(
        self, acceptance_criteria: dict[str, Any]
    ) -> dict[str, Any]:
        """
        解析验收标准

        将指标名称或 ID 解析为标准格式。委托给 TaskSubmissionService 处理。

        Args:
            acceptance_criteria: 原始验收标准

        Returns:
            解析后的验收标准
        """
        if not acceptance_criteria:
            return {}

        metric_ids = list(acceptance_criteria.keys())
        metric_result = await self.submission_service._resolve_metrics(
            evaluation_metric_ids=metric_ids,
            acceptance_criteria=acceptance_criteria,
        )

        if metric_result.get("error"):
            logger.warning(
                f"[TaskSubmitOrchestrator] 指标解析失败 | error={metric_result['error']}"
            )
            return acceptance_criteria

        resolved_criteria = {}
        for metric_id in metric_result["evaluation_metric_ids"]:
            config = acceptance_criteria.get(metric_id, {})
            if isinstance(config, dict) and "input_params" in config:
                resolved_criteria[metric_id] = config
            else:
                resolved_criteria[metric_id] = {"input_params": config}

        for metric_ref, config in acceptance_criteria.items():
            if metric_ref not in metric_result["evaluation_metric_ids"]:
                resolved_criteria[metric_ref] = config

        return resolved_criteria

    async def _record_agent_call(
        self,
        target_id: str,
        agent: AgentConfig,
        goal: dict[str, Any],
        parent_agent_level: int,
        kwargs: dict[str, Any],
    ) -> str:
        """
        记录 Agent 调用

        Args:
            target_id: 目标 Agent ID
            agent: Agent 配置
            goal: 任务目标
            parent_agent_level: 父 Agent 层级
            kwargs: 其他参数

        Returns:
            执行记录 ID
        """
        from src.services.agent_call_recorder import get_agent_call_recorder

        recorder = get_agent_call_recorder(self.session)
        instruction = goal.get("description", goal.get("title", ""))

        execution_id = await recorder.start_call(
            caller_level=f"L{parent_agent_level}",
            target_agent_id=target_id,
            target_agent_name=agent.name,
            operation_type="task_submit",
            instruction=instruction,
            instruction_summary=goal.get("title", "")[:100],
            context={
                "task_scope": kwargs.get("task_scope"),
                "evaluation_metric_count": len(kwargs.get("acceptance_criteria", {})),
                "parent_task_id": kwargs.get("parent_task_id"),
            },
            timeout=kwargs.get("timeout", 300),
            priority=kwargs.get("priority", "normal"),
        )

        await recorder.update_running(execution_id)
        return execution_id

    async def _submit_task(
        self,
        goal: dict[str, Any],
        acceptance_criteria: dict[str, Any],
        target_type: str,
        target_id: str,
        target_name: str | None,
        kwargs: dict[str, Any],
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        """
        提交任务

        Args:
            goal: 任务目标
            acceptance_criteria: 验收标准
            target_type: 目标类型
            target_id: 目标 ID
            target_name: 目标名称
            kwargs: 其他参数
            execution_id: 执行记录 ID

        Returns:
            提交结果
        """
        metadata = kwargs.get("metadata") or {}
        if execution_id:
            metadata["agent_call_execution_id"] = execution_id

        return await self.submission_service.submit(
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            user_id=kwargs.get("user_id"),
            parent_task_id=kwargs.get("parent_task_id"),
            session_id=kwargs.get("session_id"),
            task_type=kwargs.get("task_scope", "short_term"),
            priority=kwargs.get("priority", 5),
            max_retries=kwargs.get("max_retries", 3),
            metadata={
                **metadata,
                "task_scope": kwargs.get("task_scope"),
                "execution_mode": kwargs.get("execution_mode", "immediate"),
                "needs_preparation": kwargs.get("needs_preparation"),
                "use_todo": kwargs.get("use_todo"),
                "execution_record_id": kwargs.get("tool_record_id") or kwargs.get("execution_record_id"),
            },
            dependencies=kwargs.get("dependencies"),
        )

    async def _publish_task_submitted_event(
        self,
        task_id: str,
        target_type: str,
        target_id: str,
    ) -> None:
        """
        发布任务提交事件（事件驱动）

        Args:
            task_id: 任务 ID
            target_type: 目标类型
            target_id: 目标 ID
        """
        from src.core.event_bus import get_event_bus
        from src.core.event_bus.types import EventType, ExecutionEvent

        event_bus = get_event_bus()
        event = ExecutionEvent(
            event_type=EventType.TASK_SUBMITTED,
            session_id=f"task_{task_id}",
            data={
                "task_id": task_id,
                "target_type": target_type,
                "target_id": target_id,
            },
        )
        await event_bus.publish(event)
        logger.info(
            f"[TaskSubmitOrchestrator] 任务提交事件已发布 | task_id={task_id}"
        )

    async def _complete_agent_call(
        self,
        execution_id: str,
        task_id: str,
    ) -> None:
        """
        完成 Agent 调用记录

        Args:
            execution_id: 执行记录 ID
            task_id: 任务 ID
        """
        from src.services.agent_call_recorder import get_agent_call_recorder

        recorder = get_agent_call_recorder(self.session)

        await recorder.complete_call(
            execution_id=execution_id,
            success=True,
            result={"task_id": task_id},
            result_summary=f"任务已提交: {task_id}",
        )

    async def _generate_workflow_execution_id(self, kwargs: dict[str, Any]) -> str:
        """
        获取工作流执行记录 ID

        Args:
            kwargs: 参数字典

        Returns:
            执行记录 ID（直接使用 tool_record_id）
        """
        return kwargs.get("tool_record_id") or kwargs.get("metadata", {}).get("tool_record_id")

    def _build_agent_result(
        self,
        submit_result: dict[str, Any],
        agent: AgentConfig,
    ) -> ToolExecutionResult:
        """
        构建 Agent 提交结果

        Args:
            submit_result: 提交结果
            agent: Agent 配置

        Returns:
            ToolExecutionResult
        """
        result_data = {
            **submit_result,
            "target_name": agent.name,
            "execution_started": True,
        }

        return create_success_result(
            data=result_data,
            metadata={"action": "submit_to_agent"},
        )

    def _build_workflow_result(
        self,
        submit_result: dict[str, Any],
    ) -> ToolExecutionResult:
        """
        构建工作流提交结果

        Args:
            submit_result: 提交结果

        Returns:
            ToolExecutionResult
        """
        result_data = {
            **submit_result,
            "execution_started": True,
        }

        return create_success_result(
            data=result_data,
            metadata={"action": "submit_to_workflow"},
        )

    async def _get_available_agents(self, parent_agent_level: int = 1) -> list:
        """
        获取可用的 Agent 列表（根据调用者层级过滤）

        Args:
            parent_agent_level: 调用者层级（1 或 2）

        Returns:
            Agent 信息列表
        """
        try:
            allowed_targets = self.level_controller.get_allowed_targets(parent_agent_level)
            if not allowed_targets:
                return []

            result = await self.session.execute(
                select(AgentConfig).where(
                    AgentConfig.is_active,
                    AgentConfig.level.in_(allowed_targets)
                ).limit(10)
            )
            agents = result.scalars().all()
            return [{"config_id": a.config_id, "name": a.name, "level": a.level} for a in agents]
        except Exception:
            return []
