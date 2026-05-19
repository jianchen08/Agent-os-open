"""
任务执行器

负责执行通过 task_submit 工具提交的任务：
1. 从数据库读取任务信息
2. 加载目标 Agent 配置
3. 创建 AgentLoop 并执行
4. 更新任务状态和进度
5. 发送执行事件通知上层

会话管理策略：
- 使用统一的 SessionManager 管理会话生命周期
- 每个任务执行使用独立的会话
- 事务边界清晰：任务状态更新在独立事务中完成

事件驱动改造：
- 订阅 task.execution_requested 事件
- 移除 nested_executor_callback
- 嵌套任务通过发布事件触发

迁移说明：
- 原位置: src/tasks/task_executor.py
- 新位置: src/agents/task_runner.py
- 迁移时间: 2026-02-27
- 迁移原因: TaskExecutor 包含 Agent 调用逻辑，应归属 agents 模块
"""

import logging
import time
from datetime import UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.types import AgentConfig, AgentLevel, AgentType
from src.core.event_bus.types import EventFilter, EventType
from src.core.states import ExecutionStatus
from src.db.models import AgentConfig as AgentConfigModel
from src.db.models import Task
from src.db.session_manager import get_session_manager, managed_session
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class TaskRunner:
    """
    任务执行器

    负责执行任务并管理任务生命周期。
    使用统一的会话管理策略，通过 SessionManager 获取和释放会话。

    会话管理原则：
    1. 每个 execute_task_with_result 调用使用独立会话
    2. 不使用自定义会话池（依赖 SQLAlchemy 连接池）
    3. 会话生命周期由上下文管理器控制
    4. 支持向后兼容的 session 参数（但会创建新会话）

    事件驱动改造：
    - 订阅 task.execution_requested 事件
    - 嵌套任务通过发布事件触发

    BUG-FIX-fix_20260226_210500_pool: 添加任务执行去重和重试限制
    问题根因: 事件驱动重试风暴导致数据库连接池耗尽
    修复方案: 添加执行中任务追踪和失败重试次数限制
    影响范围: 任务执行系统
    """

    MAX_RETRIES = 3

    def __init__(self, session: AsyncSession | None = None):
        """
        初始化任务执行器

        Args:
            session: 数据库会话（可选，向后兼容参数，实际不使用）
        """
        self._session_manager = get_session_manager()
        self._event_bus = None
        self._subscription_id = None
        self._executing_tasks: set[str] = set()
        self._failed_tasks: dict[str, int] = {}

    async def start(self):
        """
        启动任务执行器（事件驱动改造）

        订阅 task.execution_requested 事件
        """
        from src.core.event_bus import get_event_bus

        self._event_bus = get_event_bus()

        # 订阅任务执行请求事件
        self._subscription_id = self._event_bus.subscribe(
            self._on_execution_requested,
            filter=EventFilter(event_types=[EventType.TASK_EXECUTION_REQUESTED]),
        )

        logger.info("[TaskRunner] 已订阅 task.execution_requested 事件")

    async def stop(self):
        """
        停止任务执行器（事件驱动改造）

        取消事件订阅
        """
        if self._subscription_id and self._event_bus:
            await self._event_bus.unsubscribe(self._subscription_id)
            self._subscription_id = None
            self._event_bus = None
            logger.info("[TaskRunner] 已取消订阅")

    async def _on_execution_requested(self, event):
        """
        任务执行请求事件处理（事件驱动改造）

        BUG-FIX-fix_20260226_210500_pool: 添加去重和重试限制
        - 检查任务是否已在执行中（防止重复执行）
        - 检查失败重试次数（防止无限重试）

        恢复支持: 如果事件来源是 "recovery"，清除执行追踪状态

        Args:
            event: ExecutionEvent 对象
        """
        task_id = event.data.get("task_id")
        if not task_id:
            logger.warning("[TaskRunner] 收到无效的执行请求事件 | 缺少 task_id")
            return

        source = event.data.get("source", "")

        if source == "recovery":
            self._executing_tasks.discard(task_id)
            if task_id in self._failed_tasks:
                del self._failed_tasks[task_id]
            logger.info(f"[TaskRunner] 恢复任务，清除执行追踪状态 | task_id={task_id}")

        if task_id in self._executing_tasks:
            logger.warning(
                f"[TaskRunner] 任务已在执行中，跳过重复请求 | task_id={task_id}"
            )
            return

        fail_count = self._failed_tasks.get(task_id, 0)
        if fail_count >= self.MAX_RETRIES:
            logger.error(
                f"[TaskRunner] 任务重试次数超限({fail_count}/{self.MAX_RETRIES})，放弃执行 | task_id={task_id}"
            )
            return

        logger.info(f"[TaskRunner] 收到执行请求 | task_id={task_id} | source={source}")
        await self.execute_task(task_id)

    async def execute_task(self, task_id: str) -> None:
        """
        执行任务（无返回值版本，向后兼容）

        Args:
            task_id: 任务 ID
        """
        await self.execute_task_with_result(task_id)

    async def execute_task_with_result(self, task_id: str) -> dict:
        """
        执行任务并返回结果

        使用独立的会话执行整个任务流程，确保：
        1. 会话隔离：不受调用者会话状态影响
        2. 事务完整：任务状态更新原子性
        3. 资源释放：会话始终正确关闭

        BUG-FIX-fix_20260226_210500_pool: 添加执行追踪
        - 执行前添加到执行中集合
        - 执行后从执行中集合移除
        - 失败时增加失败计数

        Args:
            task_id: 任务 ID

        Returns:
            执行结果字典，包含 success、output、error、task_id 等字段
        """
        logger.info(f"[TaskRunner] ===== 开始执行任务 ===== | task_id={task_id}")
        start_time = time.time()

        self._executing_tasks.add(task_id)
        try:
            async with managed_session() as session:
                result = await self._execute_task_internal(
                    task_id=task_id,
                    session=session,
                    start_time=start_time,
                )
                if not result.get("success"):
                    self._failed_tasks[task_id] = self._failed_tasks.get(task_id, 0) + 1
                return result
        except Exception as e:
            self._failed_tasks[task_id] = self._failed_tasks.get(task_id, 0) + 1
            logger.exception(
                f"[TaskRunner] 任务执行异常 | task_id={task_id} | error={str(e)}"
            )
            return {
                "success": False,
                "error": f"任务执行异常: {str(e)}",
                "task_id": task_id,
            }
        finally:
            self._executing_tasks.discard(task_id)

    async def _execute_task_internal(
        self,
        task_id: str,
        session: AsyncSession,
        start_time: float,
    ) -> dict:
        """
        内部任务执行逻辑

        Args:
            task_id: 任务 ID
            session: 数据库会话（由上下文管理器管理）
            start_time: 开始时间戳

        Returns:
            执行结果字典
        """
        # 1. 读取任务信息
        task = await self._load_task(task_id, session)
        if not task:
            logger.error(f"[TaskRunner] 任务不存在 | task_id={task_id}")
            return {
                "success": False,
                "error": "任务不存在",
                "task_id": task_id,
            }

        logger.info(
            f"[TaskRunner] 任务信息 | "
            f"title={task.title} | "
            f"target_id={task.target_id} | "
            f"target_type={task.target_type}"
        )

        # 2. 验证任务状态
        if task.status not in (
            ExecutionStatus.PENDING.value,
            ExecutionStatus.RUNNING.value,
        ):
            logger.warning(
                f"[TaskRunner] 任务状态不可执行，跳过 | "
                f"task_id={task_id} | status={task.status}"
            )
            return {
                "success": False,
                "error": f"任务状态不可执行: {task.status}",
                "task_id": task_id,
                "task_title": task.title,
            }

        if task.status == ExecutionStatus.RUNNING.value:
            logger.info(
                f"[TaskRunner] 任务状态为 running，可能是恢复执行 | task_id={task_id}"
            )

        try:
            # 3. 更新任务状态为 running
            await self._update_task_status(
                task_id, ExecutionStatus.RUNNING.value, session
            )
            logger.info(f"[TaskRunner] 任务状态已更新为 running | task_id={task_id}")

            # 4. 发送执行开始事件
            await self._send_execution_start(
                task_id=task_id,
                task=task,
                user_id=task.user_id,
            )

            # 5. 加载目标 Agent 配置
            if not task.target_id:
                error_msg = "任务目标 ID (target_id) 为空，无法执行"
                logger.error(f"[TaskRunner] {error_msg}")
                await self._update_task_status(
                    task_id,
                    "failed",
                    session,
                    error_message=error_msg,
                    start_time=start_time,
                )
                await self._send_execution_done(
                    task_id=task_id,
                    user_id=task.user_id,
                    success=False,
                    error=error_msg,
                    start_time=start_time,
                )
                return {
                    "success": False,
                    "error": error_msg,
                    "task_id": task_id,
                    "task_title": task.title,
                }

            if task.target_type != "agent":
                error_msg = f"暂不支持的目标类型: {task.target_type}"
                logger.error(f"[TaskRunner] {error_msg}")
                await self._update_task_status(
                    task_id,
                    "failed",
                    session,
                    error_message=error_msg,
                    start_time=start_time,
                )
                await self._send_execution_done(
                    task_id=task_id,
                    user_id=task.user_id,
                    success=False,
                    error=error_msg,
                    start_time=start_time,
                )
                return {
                    "success": False,
                    "error": error_msg,
                    "task_id": task_id,
                    "task_title": task.title,
                }

            agent_config = await self._load_agent_config(task.target_id, session)
            if not agent_config:
                error_msg = f"Agent 配置不存在: {task.target_id}"
                logger.error(f"[TaskRunner] {error_msg}")
                await self._update_task_status(
                    task_id,
                    "failed",
                    session,
                    error_message=error_msg,
                    start_time=start_time,
                )
                await self._send_execution_done(
                    task_id=task_id,
                    user_id=task.user_id,
                    success=False,
                    error=error_msg,
                    start_time=start_time,
                )
                return {
                    "success": False,
                    "error": error_msg,
                    "task_id": task_id,
                    "task_title": task.title,
                }

            logger.info(
                f"[TaskRunner] Agent 配置加载成功 | "
                f"name={agent_config.name} | "
                f"model={agent_config.model_name}"
            )

            # 6. 构建用户输入
            user_input = self._build_user_input(task, agent_config)

            # 7. 创建 AgentLoop 并执行
            result = await self._execute_agent(
                agent_config=agent_config,
                user_input=user_input,
                user_id=task.user_id,
                session_id=task.session_id,
                db_session=session,
                task_id=task_id,
                execution_record_id=task.execution_record_id,
            )

            # 8. 更新任务状态并发送完成事件
            if result.success:
                await self._update_task_status(
                    task_id, "completed", session, start_time=start_time
                )
                output_preview = result.output[:200] if result.output else ""
                logger.info(
                    f"[TaskRunner] 任务执行成功 | "
                    f"task_id={task_id} | "
                    f"output_preview={output_preview}..."
                )
                await self._send_execution_done(
                    task_id=task_id,
                    user_id=task.user_id,
                    success=True,
                    output={"result": result.output} if result.output else None,
                    start_time=start_time,
                    summary=f"任务 '{task.title}' 执行成功",
                )
                return {
                    "success": True,
                    "output": result.output,
                    "task_id": task_id,
                    "task_title": task.title,
                    "summary": f"任务 '{task.title}' 执行成功",
                }
            else:
                await self._update_task_status(
                    task_id,
                    "failed",
                    session,
                    error_message=result.error,
                    start_time=start_time,
                )
                logger.error(
                    f"[TaskRunner] 任务执行失败 | "
                    f"task_id={task_id} | "
                    f"error={result.error}"
                )
                await self._send_execution_done(
                    task_id=task_id,
                    user_id=task.user_id,
                    success=False,
                    error=result.error,
                    start_time=start_time,
                )
                return {
                    "success": False,
                    "error": result.error,
                    "task_id": task_id,
                    "task_title": task.title,
                }

        except Exception as e:
            logger.exception(
                f"[TaskRunner] 任务执行异常 | task_id={task_id} | error={str(e)}"
            )
            await self._update_task_status(
                task_id,
                "failed",
                session,
                error_message=str(e),
                start_time=start_time,
            )
            await self._send_execution_done(
                task_id=task_id,
                user_id=task.user_id if task else None,
                success=False,
                error=str(e),
                start_time=start_time,
            )
            return {
                "success": False,
                "error": str(e),
                "task_id": task_id,
                "task_title": task.title if task else None,
            }

    async def _load_task(self, task_id: str, session: AsyncSession) -> Task | None:
        """
        从数据库加载任务

        Args:
            task_id: 任务 ID
            session: 数据库会话

        Returns:
            Task 对象，不存在返回 None
        """
        stmt = select(Task).where(Task.id == task_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _load_agent_config(
        self, agent_id: str | None, session: AsyncSession
    ) -> AgentConfig | None:
        """
        从数据库加载 Agent 配置

        支持通过 ID、config_id 或名称查找 Agent

        Args:
            agent_id: Agent ID、config_id 或名称
            session: 数据库会话

        Returns:
            AgentConfig 对象，不存在返回 None
        """
        if not agent_id:
            return None

        # 首先尝试通过 ID 查找
        stmt = select(AgentConfigModel).where(AgentConfigModel.id == agent_id).limit(1)
        result = await session.execute(stmt)
        agent_model = result.scalar_one_or_none()

        # 如果通过 ID 未找到，尝试通过 config_id 查找
        if not agent_model:
            stmt = (
                select(AgentConfigModel)
                .where(AgentConfigModel.config_id == agent_id)
                .limit(1)
            )
            result = await session.execute(stmt)
            agent_model = result.scalar_one_or_none()
            if agent_model:
                logger.info(
                    f"[TaskRunner] 通过 config_id 找到 Agent | "
                    f"config_id={agent_id} -> id={agent_model.id}"
                )

        # 如果通过 config_id 未找到，尝试通过名称查找
        if not agent_model:
            logger.debug(
                f"[TaskRunner] 通过 ID/config_id 未找到 Agent，尝试通过名称查找 | "
                f"agent_id={agent_id}"
            )
            stmt = (
                select(AgentConfigModel)
                .where(AgentConfigModel.name == agent_id)
                .limit(1)
            )
            result = await session.execute(stmt)
            agent_model = result.scalar_one_or_none()
            if agent_model:
                logger.info(
                    f"[TaskRunner] 通过名称找到 Agent | "
                    f"name={agent_id} -> id={agent_model.id}"
                )

        if not agent_model:
            return None

        # 将数据库模型转换为 AgentConfig
        # 数据库 level 是 int，需要转换为 AgentLevel 枚举
        level_enum = AgentLevel.USER
        if agent_model.level:
            level_map = {1: AgentLevel.L1, 2: AgentLevel.L2, 3: AgentLevel.L3}
            level_enum = level_map.get(agent_model.level, AgentLevel.USER)

        return AgentConfig(
            name=agent_model.name,
            model_name=agent_model.model_name,
            model_params=agent_model.model_params,
            system_prompt=agent_model.system_prompt,
            tool_ids=agent_model.tool_ids or [],
            max_iterations=agent_model.max_iterations,
            timeout_seconds=agent_model.timeout_seconds,
            description=agent_model.description,
            agent_type=self._map_agent_type(agent_model.agent_type),
            level=level_enum,
            tags=agent_model.tags or [],
            metadata=agent_model.agent_metadata or {},
        )

    def _map_agent_type(self, agent_type: str) -> AgentType:
        """
        映射 Agent 类型

        Args:
            agent_type: 数据库中的 agent_type 字符串

        Returns:
            AgentType 枚举值
        """
        type_map = {
            "atomic": AgentType.ATOMIC,
            "main": AgentType.MAIN,
            "sub": AgentType.SUB,
            "subagent": AgentType.SUBAGENT,
            "specialized": AgentType.SPECIALIZED,
            "system": AgentType.SYSTEM,
        }
        return type_map.get(agent_type.lower(), AgentType.ATOMIC)

    def _build_user_input(
        self, task: Task, agent_config: AgentConfig | None = None
    ) -> str:
        """
        构建用户输入（任务目标）

        Args:
            task: 任务对象
            agent_config: 执行者配置

        Returns:
            用户输入字符串
        """
        parts = []

        # 添加任务 ID
        parts.append(f"【任务 ID】{task.id}")

        # 添加任务标题
        parts.append(f"【任务】{task.title}")

        # 添加任务描述
        if task.description:
            parts.append(f"【描述】{task.description}")

        # 添加任务目标
        if task.goal:
            if isinstance(task.goal, dict):
                goal_text = task.goal.get("title", "") or task.goal.get(
                    "description", ""
                )
                if goal_text:
                    parts.append(f"【目标】{goal_text}")

        # 添加验收标准
        acceptance_criteria = self._get_acceptance_criteria(task)
        if acceptance_criteria:
            parts.append("")
            parts.append("【验收标准】")
            for metric_id, params in acceptance_criteria.items():
                param_str = f" {params}" if params else ""
                parts.append(f"  - {metric_id}:{param_str}")

        # 添加执行要求
        parts.append("")
        agent_level = agent_config.level if agent_config else None
        use_todo = self._should_use_todo(task)

        if agent_level == AgentLevel.L3:
            parts.append(self._get_l3_execution_guide(task))
        elif agent_level == AgentLevel.L2:
            if use_todo:
                parts.append(self._get_todo_execution_guide(task))
            else:
                parts.append(self._get_l2_execution_guide(task))
        # L1 或其他层级不需要执行指引

        return "\n".join(parts)

    def _should_use_todo(self, task: Task) -> bool:
        """
        判断任务是否使用 TODO 管理

        Args:
            task: 任务对象

        Returns:
            是否使用 TODO 管理
        """
        # 1. 检查任务元数据
        if hasattr(task, "task_metadata") and task.task_metadata:
            if isinstance(task.task_metadata, dict):
                use_todo = task.task_metadata.get("use_todo")
                if use_todo is not None:
                    return bool(use_todo)

        # 2. 检查任务目标
        if task.goal and isinstance(task.goal, dict):
            use_todo = task.goal.get("use_todo")
            if use_todo is not None:
                return bool(use_todo)

        # 3. 验收标准数量 >= 3 时自动启用
        if (
            hasattr(task, "acceptance_criteria")
            and task.acceptance_criteria
            and len(task.acceptance_criteria) >= 3
        ):
            return True

        # 4. 默认不启用
        return False

    def _should_use_preparation(self, task: Task) -> bool:
        """
        判断任务是否需要准备阶段

        Args:
            task: 任务对象

        Returns:
            是否需要准备阶段
        """
        # 1. 检查任务元数据
        if hasattr(task, "task_metadata") and task.task_metadata:
            if isinstance(task.task_metadata, dict):
                needs_prep = task.task_metadata.get("needs_preparation")
                if needs_prep is not None:
                    return bool(needs_prep)

        # 2. 检查任务目标
        if task.goal and isinstance(task.goal, dict):
            needs_prep = task.goal.get("needs_preparation")
            if needs_prep is not None:
                return bool(needs_prep)

        # 3. 默认不需要
        return False

    def _get_acceptance_criteria(self, task: Task) -> dict[str, Any]:
        """获取任务的验收标准"""
        # 优先从 acceptance_criteria 字段获取
        if hasattr(task, "acceptance_criteria") and task.acceptance_criteria:
            if isinstance(task.acceptance_criteria, dict):
                return task.acceptance_criteria

        # 兼容旧版：从 evaluation_metric_ids 转换
        if hasattr(task, "evaluation_metric_ids") and task.evaluation_metric_ids:
            return {metric_id: {} for metric_id in task.evaluation_metric_ids}

        return {}

    def _get_l3_execution_guide(self, task: Task) -> str:
        """生成 L3 执行者执行步骤指南"""
        acceptance_criteria = self._get_acceptance_criteria(task)

        criteria_str = ""
        if acceptance_criteria:
            criteria_list = [f"  - {k}: {v}" for k, v in acceptance_criteria.items()]
            criteria_str = f"\n验收标准:\n{chr(10).join(criteria_list)}"

        return f"""【执行指引】

你是执行者，负责完成具体任务：

1. **制定计划** - 列出执行步骤（目标、产出）
2. **逐步执行** - 使用工具完成操作
3. **完成报告** - 总结产出和结果{criteria_str}

示例格式：
📋 执行计划
1. [步骤名称] - 目标: [...] 产出: [...]

执行步骤 1/N: ...
✅ 已完成

✅ 任务完成报告 - 产出物: [...] - 总结: [...]

**完成后调用：** task_evaluate(action='auto_complete', task_id='{task.id}')"""

    def _get_l2_execution_guide(self, task: Task) -> str:
        """生成 L2 Agent 执行指南"""
        needs_preparation = self._should_use_preparation(task)

        if needs_preparation:
            return self._get_preparation_guide(task)
        else:
            return self._get_l2_simple_guide(task)

    def _get_todo_execution_guide(self, task: Task) -> str:
        """生成 TODO 管理执行指引"""
        return f"""【执行要求】
本任务需要使用 TODO 管理工具进行步骤跟踪：

1. 先调用 todo_manage(action='write', task_id='{task.id}') 创建执行步骤列表
2. 每完成一步调用 todo_manage(action='update') 更新状态
3. 全部完成后调用 task_evaluate(action='auto_complete', task_id='{task.id}') 提交评估

**注意**：
- 步骤列表应该清晰、具体、可验证
- 每步完成后及时更新状态
- 只有所有步骤都完成并验证后才调用 task_evaluate"""

    def _get_preparation_guide(self, task: Task) -> str:
        """生成准备阶段指引（复杂任务）"""
        return """【执行方式】
本任务较复杂，需要准备阶段：

1. 先提交准备任务给 "task_preparation_executor"（L3 准备执行者）完成调研和分解
2. 等待准备完成后，根据结果逐个提交执行任务给相应的 L3 Agent
3. 汇总结果并完成任务"""

    def _get_l2_simple_guide(self, task: Task) -> str:
        """生成 L2 简单任务执行指引"""
        return f"""【执行指引】

你是任务调度者，负责分解和调度：

1. **任务分析**
   - 判断任务是否需要分解为子任务
   - 确定子任务间的依赖关系

2. **任务调度**
   - 使用 task_submit 提交子任务给执行者
   - 选择合适的执行者（backend_executor、frontend_executor 等）
   - 按依赖关系串行或并行调度

3. **完成评估**
   - 汇总结果后调用 task_evaluate(action='auto_complete', task_id='{task.id}')"""

    async def _execute_agent(
        self,
        agent_config: AgentConfig,
        user_input: str,
        user_id: str | None,
        session_id: str | None,
        db_session: AsyncSession,
        task_id: str | None = None,
        execution_record_id: str | None = None,
    ) -> Any:
        """
        执行 Agent

        Args:
            agent_config: Agent 配置
            user_input: 用户输入
            user_id: 用户 ID
            session_id: 会话 ID
            db_session: 数据库会话
            task_id: 任务 ID
            execution_record_id: 执行记录 ID

        Returns:
            AgentResult 对象
        """
        # 创建工具注册表和执行器
        tool_registry = ToolRegistry()
        tool_executor = ToolExecutor(registry=tool_registry)

        # 注册内置工具
        from src.tools.builtin import register_all_builtin_tools

        registered_tools = register_all_builtin_tools(
            registry=tool_registry,
            session=db_session,
            evaluator_callback=None,
        )
        logger.debug(f"[TaskRunner._execute_agent] 已注册内置工具: {registered_tools}")

        # 创建 AgentLoop
        from src.agents.builder import create_agent_loop

        if not session_id:
            raise ValueError(
                "必须提供 session_id，请使用 thread-{user_id_short}-{session_seq} 格式"
            )

        agent_loop = await create_agent_loop(
            config=agent_config,
            tool_registry=tool_registry,
            tool_executor=tool_executor,
            user_id=user_id,
            session_id=session_id,
            db_session=db_session,
            enable_learning=False,
            enable_monitoring=False,
            enable_checkpointing=False,
            enable_approval=False,
            extra_context=(
                {
                    "task_id": task_id,
                    "execution_record_id": execution_record_id,
                }
                if task_id or execution_record_id
                else None
            ),
        )

        # 关键：在 AgentLoop 创建后，将用户输入添加到 LayeredContextStore
        # 这样 ContextBuilder 才能加载到用户消息
        if (
            user_input
            and hasattr(agent_loop, "_layered_context_store")
            and agent_loop._layered_context_store
        ):
            try:
                layered_context_store = agent_loop._layered_context_store

                # 添加用户消息到 LayeredContextStore（同时保存到数据库）
                message = {
                    "role": "user",  # 使用 role 而不是 type
                    "content": user_input,
                    "executor": {
                        "type": "user",
                        "id": user_id or "system",
                        "name": "TaskSubmitter",
                    },
                }

                await layered_context_store.add_message(
                    message=message,
                    persist_to_db=True,  # 保存到数据库
                )

                logger.info(
                    f"[TaskRunner] 任务目标已添加到 LayeredContextStore | content_length={len(user_input)}"
                )
            except Exception as e:
                logger.warning(
                    f"[TaskRunner] 添加任务目标到 LayeredContextStore 时出错: {e}，继续执行"
                )
                # 不影响主流程，继续执行

        # 执行 Agent
        result = await agent_loop.run(user_input)

        # 清理资源
        await agent_loop.cleanup()

        return result

    async def _update_task_status(
        self,
        task_id: str,
        status: str,
        session: AsyncSession,
        error_message: str | None = None,
        start_time: float | None = None,
    ) -> None:
        """
        更新任务状态

        Args:
            task_id: 任务 ID
            status: 新状态
            session: 数据库会话
            error_message: 错误信息（可选）
            start_time: 任务开始时间（可选）
        """
        from datetime import datetime

        update_values = {"status": status}

        # 根据状态设置时间戳
        if status == ExecutionStatus.RUNNING.value:
            update_values["started_at"] = datetime.now(UTC)
        elif status in (ExecutionStatus.COMPLETED.value, ExecutionStatus.FAILED.value):
            update_values["completed_at"] = datetime.now(UTC)
            # 如果 started_at 还没设置，也设置它
            stmt_check = select(Task).where(Task.id == task_id)
            result_check = await session.execute(stmt_check)
            task = result_check.scalar_one_or_none()
            if task and task.started_at is None:
                update_values["started_at"] = datetime.now(UTC)

        # 如果有错误信息，存储在 metadata 中
        if error_message is not None:
            stmt_check = select(Task).where(Task.id == task_id)
            result_check = await session.execute(stmt_check)
            task = result_check.scalar_one_or_none()
            if task:
                existing_metadata = task.task_metadata or {}
                existing_metadata["error_message"] = error_message
                update_values["metadata"] = existing_metadata

        # 使用直接 SQL 更新避免 SQLAlchemy ORM 兼容性问题
        import json

        from sqlalchemy import text

        set_clauses = []
        params = {"task_id": task_id}
        for key, value in update_values.items():
            set_clauses.append(f"{key} = :{key}")
            # JSON 类型需要序列化
            if key == "metadata" and isinstance(value, dict):
                params[key] = json.dumps(value)
            else:
                params[key] = value

        sql = f"UPDATE tasks SET {', '.join(set_clauses)} WHERE id = :task_id"
        await session.execute(text(sql), params)
        await session.commit()

        # BUG-FIX-fix_20260226_duplicate_event: 移除重复的事件发布
        # 问题根因: Scheduler 和 TaskExecutor 都在发布 task.completed/task.failed 事件
        # 导致同一个任务被处理两次，触发 _on_task_completed 和 _on_task_failed
        # 修复方案: 由 Scheduler 统一发布事件，TaskExecutor 只更新数据库状态
        # 注意: Scheduler 在 _execute_task_internal 的 finally 块中发布事件

    async def _send_execution_start(
        self,
        task_id: str,
        task: Task,
        user_id: str | None,
    ) -> None:
        """
        发送执行开始事件

        Args:
            task_id: 任务 ID
            task: 任务对象
            user_id: 用户 ID
        """
        if not user_id:
            return

        try:
            from src.api.websocket.service import get_event_service

            event_service = get_event_service()
            if event_service:
                await event_service.send_execution_start(
                    user_id=user_id,
                    execution_id=task_id,
                    execution_type="agent",
                    name=task.title or "任务执行",
                    description=task.description,
                    parent_id=task.parent_task_id,
                    input_data={
                        "target_type": task.target_type,
                        "target_id": task.target_id,
                    },
                    metadata={
                        "session_id": task.session_id,
                    },
                )
        except Exception as e:
            logger.warning(
                f"[TaskRunner] 发送执行开始事件失败 | task_id={task_id} | error={e}"
            )

    async def _send_execution_done(
        self,
        task_id: str,
        user_id: str | None,
        success: bool,
        output: dict | None = None,
        error: str | None = None,
        start_time: float | None = None,
        summary: str | None = None,
    ) -> None:
        """
        发送执行完成事件

        Args:
            task_id: 任务 ID
            user_id: 用户 ID
            success: 是否成功
            output: 输出结果
            error: 错误信息
            start_time: 开始时间戳
            summary: 执行摘要
        """
        if not user_id:
            return

        try:
            from src.api.websocket.service import get_event_service

            event_service = get_event_service()
            if event_service:
                duration_ms = None
                if start_time:
                    duration_ms = int((time.time() - start_time) * 1000)

                await event_service.send_execution_done(
                    user_id=user_id,
                    execution_id=task_id,
                    success=success,
                    output=output,
                    error=error,
                    duration_ms=duration_ms,
                    summary=summary,
                )
        except Exception as e:
            logger.warning(
                f"[TaskRunner] 发送执行完成事件失败 | task_id={task_id} | error={e}"
            )


# 向后兼容别名
TaskExecutor = TaskRunner
