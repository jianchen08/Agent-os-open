"""
调度器模块

统一任务调度入口：
- schedule(task_id): 统一执行入口，从数据库读取任务并执行
- 移除复杂的事件链，所有任务启动都通过 schedule() 方法
"""

import asyncio
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.exceptions import SchedulerError, TaskExecutionError, TaskNotFoundError
from src.core.states import ExecutionStatus
from src.db.session_manager import managed_session
from src.orchestration.types import (
    AgentLevel,
    ResourceAllocation,
    ResourceQuota,
    TargetType,
    TaskPriority,
    TaskRequest,
)

logger = logging.getLogger(__name__)


TaskCompletionCallback = Callable[["TaskRequest", dict[str, Any]], None]


class Scheduler:
    """
    统一任务调度器

    从 GlobalAgentScheduler 扩展而来，支持调度：
    - Agent 任务
    - Workflow 任务
    - Tool 任务

    使用 ExecutorFactory 根据 target_type 选择对应的执行器。
    """

    def __init__(self, quota: ResourceQuota | None = None) -> None:
        """
        初始化调度器

        Args:
            quota: 资源配额配置，为 None 时使用默认配置
        """
        self.quota = quota or ResourceQuota()
        self._pending_queues: dict[AgentLevel, deque] = {
            level: deque() for level in AgentLevel
        }
        self._running_tasks: dict[str, TaskRequest] = {}
        self._allocations: dict[str, ResourceAllocation] = {}
        self._level_counters: dict[AgentLevel, int] = dict.fromkeys(AgentLevel, 0)
        self._schedule_lock = asyncio.Lock()
        self._running = False
        self._scheduler_task: asyncio.Task | None = None
        self._task_event = asyncio.Event()
        self._completion_callbacks: list[TaskCompletionCallback] = []
        self._stats = {
            "total_submitted": 0,
            "total_completed": 0,
            "total_failed": 0,
        }
        self._completed_tasks: OrderedDict[str, TaskRequest] = OrderedDict()
        self._max_completed_cache = 1000

        # 任务ID映射：task_id -> scheduler_task_id
        self._task_id_mapping: dict[str, str] = {}

        # 正在处理中的任务ID集合（防止重复提交）
        self._processing_task_ids: set[str] = set()

        # 后台任务引用管理：task_id -> asyncio.Task
        # 用于追踪正在执行的任务，防止引用丢失
        self._background_tasks: dict[str, asyncio.Task] = {}

        # 订阅事件（事件驱动改造：订阅 task.ready_for_scheduling）
        self._event_bus = get_event_bus()
        self._subscription_ids = []

        # 初始化性能监控器
        try:
            from src.monitoring import get_performance_monitor

            self._performance_monitor = get_performance_monitor()
        except ImportError:
            self._performance_monitor = None

        logger.info("统一任务调度器已初始化")

    # === 私有方法 ===

    def _find_schedulable_tasks(self) -> list[TaskRequest]:
        """查找可调度的任务"""
        all_tasks = []
        for queue in self._pending_queues.values():
            for task in queue:
                if task.status == ExecutionStatus.PENDING:
                    all_tasks.append(task)
        return self._apply_fairness_policy(all_tasks)

    def _apply_fairness_policy(self, tasks: list[TaskRequest]) -> list[TaskRequest]:
        """应用公平性策略"""
        level_loads = {}
        for level in AgentLevel:
            running = self._level_counters[level]
            max_c = getattr(self.quota, f"max_l{level.value}_agents")
            level_loads[level] = running / max_c if max_c > 0 else 0

        current_time = time.time()

        def fairness_key(t: TaskRequest) -> tuple:
            # 1. 层级优先：L1 > L2 > L3（层级值越小越优先）
            level_priority = t.agent_level.value

            # 2. 优先级权重：使用非线性权重，提高高优先级任务的优先级
            priority_weight = t.priority.value ** 1.5

            # 3. 等待时间：任务已经等待的时间，避免低优先级任务饥饿
            wait_time = current_time - t.created_at
            wait_time_factor = wait_time * (0.1 + (10 - t.priority.value) * 0.02)

            # 4. 负载均衡：优先选择负载较低的层级
            load_factor = level_loads[t.agent_level]

            # 5. 执行时间：短任务优先，提高系统吞吐量
            exec_time_factor = t.estimated_duration * (0.5 + (10 - t.priority.value) * 0.1)

            return (level_priority, -priority_weight, -wait_time_factor, load_factor, exec_time_factor)

        tasks.sort(key=fairness_key)
        return tasks

    async def _can_allocate_resource(self, level: AgentLevel) -> bool:
        """检查是否可以分配资源"""
        current = self._level_counters[level]
        max_c = getattr(self.quota, f"max_l{level.value}_agents")
        if current >= max_c:
            return False
        total = sum(self._level_counters.values())
        if total >= self.quota.max_total_agents:
            return False
        return await self._check_system_resources()

    async def _check_system_resources(self) -> bool:
        """检查系统资源"""
        try:
            import psutil  # noqa: PLC0415

            # 减少CPU检查的时间间隔，使用快速模式
            cpu = psutil.cpu_percent(interval=0.01)  # 减少到0.01秒
            if cpu > self.quota.max_cpu_percent:
                return False
            # 内存检查相对较快，保持不变
            mem = psutil.virtual_memory()
            if mem.percent > self.quota.max_memory_percent:
                return False
            return True
        except ImportError:
            return True
        except OSError as e:
            logger.error("资源检查失败: %s", e)
            return False

    async def _release_resource(self, task_id: str) -> None:
        """释放任务资源"""
        if task_id in self._allocations:
            alloc = self._allocations[task_id]
            self._level_counters[alloc.agent_level] -= 1
            del self._allocations[task_id]
        if task_id in self._running_tasks:
            completed = self._running_tasks[task_id]
            self._completed_tasks[task_id] = completed
            if len(self._completed_tasks) > self._max_completed_cache:
                oldest = next(iter(self._completed_tasks))
                del self._completed_tasks[oldest]
            del self._running_tasks[task_id]

            # 清理任务ID映射关系
            # 查找并删除该 scheduler_task_id 对应的原始 task_id 映射
            original_task_id = completed.config.get("task_id")
            if original_task_id and original_task_id in self._task_id_mapping:
                del self._task_id_mapping[original_task_id]
                logger.debug(f"清理任务ID映射 | task_id={original_task_id}")

        self._task_event.set()

    async def _cleanup_completed_tasks(self) -> None:
        """清理已完成的任务"""
        completed = []
        for task_id, task in self._running_tasks.items():
            if task.status in [
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            ]:
                completed.append(task_id)
        for task_id in completed:
            await self._release_resource(task_id)

    async def _execute_task(self, task: TaskRequest) -> None:
        """
        执行任务

        使用 ExecutorFactory 根据 target_type 选择对应的执行器。
        执行逻辑完全委托给执行器，调度器只负责资源管理和回调触发。

        核心原则：执行器不处理状态转换
        - 执行完成后，不设置任何状态（EVALUATING/FAILED 等）
        - 状态转换完全由 should_continue 机制和评估服务处理
        - 调度器只记录执行结果，不干预业务状态
        """
        result = {}
        try:
            task.status = ExecutionStatus.RUNNING
            task.started_at = time.time()
            logger.info(
                "开始执行 | task_id=%s | level=%s | target_type=%s",
                task.task_id,
                task.agent_level.name,
                task.target_type.value,
            )

            from src.orchestration.executor_factory import ExecutorFactory

            executor = ExecutorFactory.create_executor(task.target_type.value)
            result = await executor.execute_task(task)

            task.result = result
            task.completed_at = time.time()
            task.actual_duration = task.completed_at - task.started_at

            logger.info(
                "任务执行完成 | task_id=%s | duration=%.2fs | success=%s | 状态由 should_continue 机制处理",
                task.task_id,
                task.actual_duration,
                result.get("success", False),
            )

            if self._performance_monitor:
                try:
                    pending = sum(len(queue) for queue in self._pending_queues.values())
                    running = len(self._running_tasks)
                    completed = self._stats["total_completed"]
                    self._performance_monitor.update_task_status(
                        pending=pending,
                        running=running,
                        completed=completed,
                        task_time=task.actual_duration,
                    )
                except Exception as e:
                    logger.warning("记录任务指标失败: %s", e)

        except TaskExecutionError as e:
            task.error = str(e)
            task.completed_at = time.time()
            result = {"success": False, "error": str(e)}
            logger.error("任务执行错误 | task_id=%s | error=%s", task.task_id, e)
        except Exception as e:
            task.error = str(e)
            task.completed_at = time.time()
            result = {"success": False, "error": str(e)}
            logger.error("任务执行异常 | task_id=%s | error=%s", task.task_id, e)
        finally:
            original_task_id = task.config.get("task_id")
            if original_task_id:
                await self._event_bus.publish(
                    ExecutionEvent(
                        event_type=EventType.TASK_EXECUTION_REQUESTED,
                        session_id=original_task_id,
                        data={
                            "task_id": original_task_id,
                            "result": result,
                            "error": task.error,
                        },
                    )
                )

            for callback in self._completion_callbacks:
                try:
                    callback(task, result)
                except Exception:
                    logger.warning("回调执行失败 | task_id=%s", task.task_id)
            await self._release_resource(task.task_id)

    async def _schedule_task(self, task: TaskRequest) -> None:
        """调度任务执行"""
        queue = self._pending_queues[task.agent_level]
        if task in queue:
            queue.remove(task)
        task.status = ExecutionStatus.SCHEDULED
        task.scheduled_at = time.time()
        alloc = ResourceAllocation(
            task_id=task.task_id,
            agent_level=task.agent_level,
            allocated_at=time.time(),
            expected_release_at=time.time() + task.estimated_duration,
        )
        self._allocations[task.task_id] = alloc
        self._running_tasks[task.task_id] = task
        self._level_counters[task.agent_level] += 1
        logger.info(
            "任务已调度 | task_id=%s | level=%s | target_type=%s",
            task.task_id,
            task.agent_level.name,
            task.target_type.value,
        )
        # 创建后台任务并保存引用，防止垃圾回收
        exec_task = asyncio.create_task(self._execute_task(task))
        self._background_tasks[task.task_id] = exec_task
        # 添加完成回调以清理引用
        exec_task.add_done_callback(
            lambda t, tid=task.task_id: self._on_execution_task_done(tid, t)
        )

    async def _try_schedule(self) -> None:
        """尝试调度任务"""
        async with self._schedule_lock:
            await self._cleanup_completed_tasks()
            tasks = self._find_schedulable_tasks()
            for task in tasks:
                if await self._can_allocate_resource(task.agent_level):
                    await self._schedule_task(task)

    async def _scheduler_loop(self) -> None:
        """调度器主循环"""
        logger.info("调度器主循环已启动")

        # 优化：动态调整任务清理频率
        cleanup_interval = 2.0  # 初始每2秒清理一次
        last_cleanup_time = time.time()

        # 优化：批量处理调度请求
        batch_schedule_interval = 0.1  # 批量调度间隔
        last_schedule_time = time.time()

        while self._running:
            try:
                try:
                    # 减少超时时间，提高响应速度
                    await asyncio.wait_for(self._task_event.wait(), timeout=0.5)
                    self._task_event.clear()
                except TimeoutError:
                    pass

                current_time = time.time()

                # 定期清理已完成的任务
                if current_time - last_cleanup_time > cleanup_interval:
                    await self._cleanup_completed_tasks()
                    last_cleanup_time = current_time

                # 批量处理调度请求，减少调度开销
                if current_time - last_schedule_time > batch_schedule_interval:
                    await self._try_schedule()
                    last_schedule_time = current_time

            except asyncio.CancelledError:
                break
            except SchedulerError as e:
                logger.error("调度器异常: %s", e)
                await asyncio.sleep(0.2)
            except Exception as e:
                # 捕获所有异常，确保调度器不会崩溃
                logger.exception("调度器未预期异常: %s", e)
                await asyncio.sleep(0.5)

    # === 事件处理 ===

    async def _on_task_ready_for_scheduling(self, event: ExecutionEvent) -> None:
        """
        处理任务调度就绪事件（事件驱动改造）

        当 TaskOrchestrator 验证任务依赖满足后，会发布此事件。
        Scheduler 收到此事件后，将任务加入调度队列。

        Args:
            event: 任务调度就绪事件
        """
        data = event.data
        task_id = data.get("task_id")
        target_type = data.get("target_type", "agent")

        # 检查任务是否已经在处理中（防止重复提交）
        if task_id in self._processing_task_ids:
            logger.warning(f"任务已在调度队列中，忽略重复提交 | task_id={task_id}")
            return

        # 检查任务是否已映射到调度器任务（已在队列或执行中）
        if task_id in self._task_id_mapping:
            logger.warning(
                f"任务已存在映射关系，忽略重复提交 | task_id={task_id} | "
                f"scheduler_task_id={self._task_id_mapping[task_id]}"
            )
            return

        # 标记任务为处理中
        self._processing_task_ids.add(task_id)

        try:
            # 提交到调度队列
            scheduler_task_id = await self.submit_task(
                agent_level=AgentLevel(data.get("agent_level", 3)),
                description=data.get("metadata", {}).get("title", ""),
                prompt="",
                priority=TaskPriority(data.get("priority", 5)),
                target_type=TargetType(target_type),
                parent_task_id=data.get("parent_task_id"),
                session_id=data.get("session_id"),
                config={"task_id": task_id, "metadata": data.get("metadata", {})},
            )

            # 保存映射关系
            self._task_id_mapping[task_id] = scheduler_task_id

            # 发布调度事件
            await self._event_bus.publish(
                ExecutionEvent(
                    event_type=EventType.TASK_READY_FOR_SCHEDULING,
                    session_id=task_id,
                    data={
                        "task_id": task_id,
                        "scheduler_task_id": scheduler_task_id,
                        "agent_level": data.get("agent_level", 3),
                        "target_type": target_type,
                    },
                )
            )

            logger.info(
                f"任务已调度（调度就绪） | task_id={task_id} | "
                f"scheduler_task_id={scheduler_task_id} | "
                f"target_type={target_type}"
            )
        finally:
            # 移除处理中标记
            self._processing_task_ids.discard(task_id)

    async def _on_task_cancelled(self, event: ExecutionEvent) -> None:
        """
        处理任务取消事件

        Args:
            event: 任务取消事件
        """
        data = event.data
        task_id = data.get("task_id")

        # 查找调度器任务ID
        scheduler_task_id = self._task_id_mapping.get(task_id)
        if scheduler_task_id:
            # 从调度队列中移除
            await self.cancel_task(scheduler_task_id)
            del self._task_id_mapping[task_id]
            logger.info(
                f"任务已从调度器移除 | task_id={task_id} | "
                f"scheduler_task_id={scheduler_task_id}"
            )

    def _on_execution_task_done(self, task_id: str, task: asyncio.Task) -> None:
        """
        执行任务完成时的回调

        Args:
            task_id: 任务ID
            task: 完成的任务对象
        """
        # 从后台任务字典中移除引用
        if task_id in self._background_tasks:
            del self._background_tasks[task_id]

        # 检查任务是否有异常
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug(f"任务执行被取消 | task_id={task_id}")
        except Exception as e:
            logger.exception(f"任务执行异常 | task_id={task_id} | error={e}")

    # === 公共方法 ===

    async def schedule(self, task_id: str) -> dict[str, Any]:
        """
        统一任务执行入口

        从数据库读取任务并直接执行。
        所有任务启动都通过此方法：
        - API 启动任务
        - 任务提交工具
        - Watchdog 触发
        - 恢复任务

        Args:
            task_id: 任务 ID

        Returns:
            执行结果字典
        """
        logger.info(f"[Scheduler] 统一执行入口 | task_id={task_id}")

        if task_id in self._processing_task_ids:
            logger.warning(f"任务已在执行中，跳过重复提交 | task_id={task_id}")
            return {"success": False, "error": "任务已在执行中", "task_id": task_id}

        self._processing_task_ids.add(task_id)

        try:
            async with managed_session() as session:
                from sqlalchemy import select

                from src.db.models import Task

                result = await session.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()

                if not task:
                    logger.error(f"任务不存在 | task_id={task_id}")
                    return {"success": False, "error": "任务不存在", "task_id": task_id}

                if task.status not in (
                    ExecutionStatus.PENDING.value,
                    ExecutionStatus.RUNNING.value,
                ):
                    logger.warning(
                        f"任务状态不可执行 | task_id={task_id} | status={task.status}"
                    )
                    return {
                        "success": False,
                        "error": f"任务状态不可执行: {task.status}",
                        "task_id": task_id,
                    }

            logger.info(
                f"[Scheduler] 任务验证通过，准备执行 | task_id={task_id} | status={task.status}"
            )

            from src.agents.task_runner import TaskRunner

            runner = TaskRunner()
            result = await runner.execute_task_with_result(task_id)

            logger.info(
                f"[Scheduler] 任务执行完成 | task_id={task_id} | success={result.get('success')}"
            )
            return result

        except Exception as e:
            logger.exception(
                f"[Scheduler] 任务执行异常 | task_id={task_id} | error={e}"
            )
            return {"success": False, "error": str(e), "task_id": task_id}
        finally:
            self._processing_task_ids.discard(task_id)

    def register_completion_callback(self, callback: TaskCompletionCallback) -> None:
        """注册任务完成回调"""
        self._completion_callbacks.append(callback)

    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            return
        self._running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("统一任务调度器已启动")

    async def stop(self) -> None:
        """停止调度器"""
        self._running = False

        # 取消调度器主循环
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

        # 取消所有正在执行的后台任务
        if self._background_tasks:
            logger.info(f"正在取消 {len(self._background_tasks)} 个后台任务")
            for task_id, task in list(self._background_tasks.items()):
                if not task.done():
                    task.cancel()
                    logger.debug(f"已取消后台任务 | task_id={task_id}")

            # 等待所有任务完成（带超时）
            pending_tasks = [
                task for task in self._background_tasks.values() if not task.done()
            ]
            if pending_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending_tasks, return_exceptions=True),
                        timeout=5.0,
                    )
                except TimeoutError:
                    logger.warning("等待后台任务取消超时")

            self._background_tasks.clear()

        # 清空待处理队列
        for level_queue in self._pending_queues.values():
            while level_queue:
                task = level_queue.popleft()
                task.status = ExecutionStatus.CANCELLED

        logger.info("统一任务调度器已停止")

    async def submit_task(
        self,
        agent_level: AgentLevel,
        description: str,
        prompt: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        target_type: TargetType = TargetType.AGENT,
        parent_task_id: str | None = None,
        parent_record_id: str | None = None,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> str:
        """
        提交任务到调度队列

        Args:
            agent_level: Agent 层级
            description: 任务描述
            prompt: 任务提示
            priority: 任务优先级
            target_type: 目标类型（agent/workflow/tool）
            parent_task_id: 父任务ID
            parent_record_id: 父执行记录ID（用于执行记录父子关系）
            session_id: 会话ID
            config: 额外配置

        Returns:
            任务ID
        """
        task_id = str(uuid.uuid4())
        # 在 config 中传递 parent_record_id 和 session_id
        config = config or {}
        config["parent_record_id"] = parent_record_id
        config["session_id"] = session_id

        task = TaskRequest(
            task_id=task_id,
            agent_level=agent_level,
            priority=priority,
            target_type=target_type,
            parent_task_id=parent_task_id,
            session_id=session_id,
            description=description,
            prompt=prompt,
            config=config,
        )
        async with self._schedule_lock:
            self._pending_queues[agent_level].append(task)
            self._stats["total_submitted"] += 1
        logger.info(
            "任务已提交 | task_id=%s | level=%s | target_type=%s",
            task_id,
            agent_level.name,
            target_type.value,
        )
        if self._running:
            self._task_event.set()
        return task_id

    async def wait_for_completion(
        self, task_id: str, timeout: float | None = None
    ) -> TaskRequest:
        """
        等待任务完成

        Args:
            task_id: 任务ID
            timeout: 超时时间（秒）

        Returns:
            任务请求对象

        Raises:
            TaskNotFoundError: 任务不存在
            TimeoutError: 等待超时
        """
        start_time = time.time()
        while True:
            task = await self.get_task_status(task_id)
            if not task:
                raise TaskNotFoundError(f"任务不存在: {task_id}")
            if task.status in [
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            ]:
                return task
            if timeout and (time.time() - start_time) > timeout:
                raise TimeoutError(f"等待超时: {task_id}")
            await asyncio.sleep(0.1)

    async def get_task_status(self, task_id: str) -> TaskRequest | None:
        """
        获取任务状态

        Args:
            task_id: 任务ID

        Returns:
            任务请求对象，不存在时返回 None
        """
        if task_id in self._running_tasks:
            return self._running_tasks[task_id]
        for level_queue in self._pending_queues.values():
            for task in level_queue:
                if task.task_id == task_id:
                    return task
        if task_id in self._completed_tasks:
            self._completed_tasks.move_to_end(task_id)
            return self._completed_tasks[task_id]
        return None

    async def cancel_task(self, task_id: str) -> bool:
        """
        取消任务

        Args:
            task_id: 任务ID

        Returns:
            是否成功取消
        """
        async with self._schedule_lock:
            for level_queue in self._pending_queues.values():
                for i, task in enumerate(level_queue):
                    if task.task_id == task_id:
                        task.status = ExecutionStatus.CANCELLED
                        del level_queue[i]
                        logger.info("任务已取消 | task_id=%s", task_id)
                        return True
            if task_id in self._running_tasks:
                task = self._running_tasks[task_id]
                task.status = ExecutionStatus.CANCELLED
                logger.info("运行中任务已标记取消 | task_id=%s", task_id)
                return True
        return False

    async def get_next_task(self) -> TaskRequest | None:
        """
        获取下一个待执行的任务

        Returns:
            任务请求对象，没有可执行任务时返回 None
        """
        async with self._schedule_lock:
            tasks = self._find_schedulable_tasks()
            for task in tasks:
                if await self._can_allocate_resource(task.agent_level):
                    queue = self._pending_queues[task.agent_level]
                    if task in queue:
                        queue.remove(task)
                    task.status = ExecutionStatus.SCHEDULED
                    task.scheduled_at = time.time()
                    alloc = ResourceAllocation(
                        task_id=task.task_id,
                        agent_level=task.agent_level,
                        allocated_at=time.time(),
                        expected_release_at=(time.time() + task.estimated_duration),
                    )
                    self._allocations[task.task_id] = alloc
                    self._running_tasks[task.task_id] = task
                    self._level_counters[task.agent_level] += 1
                    return task
        return None

    async def report_completion(self, task_id: str, result: dict[str, Any]) -> None:
        """
        报告任务完成

        核心原则：执行器不处理状态转换
        - 只记录执行结果，不设置任何状态
        - 状态转换完全由 should_continue 机制和评估服务处理

        Args:
            task_id: 任务ID
            result: 执行结果
        """
        async with self._schedule_lock:
            if task_id not in self._running_tasks:
                logger.warning("任务不存在 | task_id=%s", task_id)
                return
            task = self._running_tasks[task_id]
            task.result = result
            task.completed_at = time.time()
            if task.started_at:
                task.actual_duration = task.completed_at - task.started_at
            logger.debug(
                f"任务执行完成 | task_id={task_id} | 状态由 should_continue 机制处理"
            )

    async def trigger_evaluation_callback(
        self, task_id: str, evaluation_result: dict[str, Any]
    ) -> None:
        """
        触发评估完成回调

        此方法由评估服务调用，评估结果中的 task_status 是评估后的最终状态。
        评估服务通过 EvaluationService.complete_task_after_evaluation() 决定任务是否完成。

        注意：此方法仅用于触发回调和释放资源，不设置状态。
        状态变更由评估服务通过 TaskStateService 完成。

        Args:
            task_id: 任务 ID
            evaluation_result: 评估结果
        """
        async with self._schedule_lock:
            if task_id not in self._running_tasks:
                logger.warning("触发回调时任务不存在 | task_id=%s", task_id)
                return

            task = self._running_tasks[task_id]

            # 触发所有注册的回调
            for callback in self._completion_callbacks:
                try:
                    callback(task, evaluation_result)
                except Exception:  # noqa: BLE001
                    logger.warning("回调执行失败")

            # 评估完成后释放资源
            await self._release_resource(task_id)

    def unregister_completion_callback(self, callback: Callable) -> None:
        """
        取消注册完成回调

        Args:
            callback: 要取消注册的回调函数
        """
        if callback in self._completion_callbacks:
            self._completion_callbacks.remove(callback)

    def get_statistics(self) -> dict[str, Any]:
        """
        获取调度器统计信息

        Returns:
            统计信息字典
        """
        return {
            **self._stats,
            "pending_tasks": {
                level.name: len(queue) for level, queue in self._pending_queues.items()
            },
            "running_tasks": len(self._running_tasks),
            "resource_usage": {
                level.name: count for level, count in self._level_counters.items()
            },
            "resource_limits": {
                "max_l1": self.quota.max_l1_agents,
                "max_l2": self.quota.max_l2_agents,
                "max_l3": self.quota.max_l3_agents,
                "max_total": self.quota.max_total_agents,
            },
        }


# === 全局调度器实例（统一执行入口）===

_global_scheduler: Scheduler | None = None


def get_global_scheduler() -> Scheduler:
    """
    获取全局调度器实例

    Returns:
        全局调度器实例
    """
    global _global_scheduler  # noqa: PLW0603
    if _global_scheduler is None:
        quota = ResourceQuota()
        _global_scheduler = Scheduler(quota)
    return _global_scheduler


async def schedule(task_id: str) -> dict[str, Any]:
    """
    统一任务执行入口

    从数据库读取任务并直接执行。
    所有任务启动都通过此方法：
    - API 启动任务
    - 任务提交工具
    - Watchdog 触发
    - 恢复任务

    Args:
        task_id: 任务 ID

    Returns:
        执行结果字典
    """
    scheduler = get_global_scheduler()
    return await scheduler.schedule(task_id)


async def start_global_scheduler() -> None:
    """启动全局调度器"""
    scheduler = get_global_scheduler()
    await scheduler.start()


async def stop_global_scheduler() -> None:
    """停止全局调度器"""
    global _global_scheduler  # noqa: PLW0603
    if _global_scheduler:
        await _global_scheduler.stop()
        _global_scheduler = None
