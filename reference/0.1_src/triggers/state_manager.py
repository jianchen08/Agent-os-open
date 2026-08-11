"""
触发器状态管理器

管理触发器的状态、执行历史和统计信息。
注意：这是纯内存实现，触发器配置从 YAML 文件加载，不需要数据库持久化。
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from src.core.states import LifecycleStatus

logger = logging.getLogger(__name__)


class TriggerStateManager:
    """
    触发器状态管理器

    负责管理触发器的状态、执行历史和统计信息。

    注意：这是纯内存实现，触发器配置从 YAML 文件加载。
    状态仅在内存中维护，服务重启后会重置。
    """

    def __init__(self):
        """初始化状态管理器"""
        self._trigger_states: dict[str, dict[str, Any]] = {}

    async def update_trigger_state(
        self,
        trigger_id: str,
        state: LifecycleStatus,
        last_execution: datetime | None = None,
        execution_count: int | None = None,
        last_error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        """
        更新触发器状态

        Args:
            trigger_id: 触发器ID
            state: 生命周期状态 (LifecycleStatus)
            last_execution: 最后执行时间
            execution_count: 执行次数
            last_error: 最后错误信息
            metadata: 元数据
        """
        now = datetime.utcnow()

        current_state = self._trigger_states.get(trigger_id, {})

        updated_state = {
            **current_state,
            "trigger_id": trigger_id,
            "state": state.value,
            "updated_at": now,
        }

        if last_execution is not None:
            updated_state["last_execution"] = last_execution

        if execution_count is not None:
            updated_state["execution_count"] = execution_count

        if last_error is not None:
            updated_state["last_error"] = last_error

        if metadata is not None:
            updated_state["metadata"] = metadata

        self._trigger_states[trigger_id] = updated_state
        logger.info(f"触发器状态已更新: {trigger_id} -> {state}")

    async def get_trigger_state(self, trigger_id: str) -> dict[str, Any] | None:
        """
        获取触发器状态

        Args:
            trigger_id: 触发器ID

        Returns:
            触发器状态信息
        """
        return self._trigger_states.get(trigger_id)

    async def list_trigger_states(
        self,
        state_filter: LifecycleStatus | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        列出触发器状态

        Args:
            state_filter: 状态过滤器（LifecycleStatus 枚举或字符串值）
            limit: 限制数量
            offset: 偏移量

        Returns:
            触发器状态列表
        """
        states = list(self._trigger_states.values())

        if state_filter:
            filter_value = state_filter.value if isinstance(state_filter, LifecycleStatus) else state_filter
            states = [s for s in states if s.get("state") == filter_value]

        states.sort(key=lambda x: x.get("updated_at", datetime.min), reverse=True)

        return states[offset : offset + limit]

    async def record_execution(
        self,
        trigger_id: str,
        success: bool,
        execution_time: float,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ):
        """
        记录触发器执行

        Args:
            trigger_id: 触发器ID
            success: 是否成功
            execution_time: 执行时间（秒）
            result: 执行结果
            error: 错误信息
        """
        now = datetime.utcnow()

        current_state = self._trigger_states.get(trigger_id, {})
        execution_count = current_state.get("execution_count", 0) + 1

        new_state = LifecycleStatus.ACTIVE if success else LifecycleStatus.ERROR

        await self.update_trigger_state(
            trigger_id=trigger_id,
            state=new_state,
            last_execution=now,
            execution_count=execution_count,
            last_error=error if not success else None,
            metadata={
                **current_state.get("metadata", {}),
                "last_execution_time": execution_time,
                "last_result": result,
            },
        )

        logger.info(f"触发器执行已记录: {trigger_id}, 成功: {success}, 耗时: {execution_time:.2f}s")

    async def get_execution_statistics(
        self, trigger_id: str | None = None, time_range: int | None = None
    ) -> dict[str, Any]:
        """
        获取执行统计信息

        Args:
            trigger_id: 触发器ID（可选，用于单个触发器统计）
            time_range: 时间范围（秒，可选）

        Returns:
            执行统计信息
        """
        if trigger_id:
            state = await self.get_trigger_state(trigger_id)
            if not state:
                return {"error": "触发器不存在"}

            return {
                "trigger_id": trigger_id,
                "state": state.get("state"),
                "execution_count": state.get("execution_count", 0),
                "last_execution": state.get("last_execution"),
                "last_error": state.get("last_error"),
                "last_execution_time": state.get("metadata", {}).get("last_execution_time"),
            }
        all_states = list(self._trigger_states.values())

        total_triggers = len(all_states)
        active_triggers = sum(1 for s in all_states if s.get("state") == LifecycleStatus.ACTIVE.value)
        error_triggers = sum(1 for s in all_states if s.get("state") == LifecycleStatus.ERROR.value)
        inactive_triggers = sum(1 for s in all_states if s.get("state") == LifecycleStatus.INACTIVE.value)

        total_executions = sum(s.get("execution_count", 0) for s in all_states)

        execution_times = [
            s.get("metadata", {}).get("last_execution_time", 0)
            for s in all_states
            if s.get("metadata", {}).get("last_execution_time")
        ]
        avg_execution_time = sum(execution_times) / len(execution_times) if execution_times else 0

        return {
            "total_triggers": total_triggers,
            "active_triggers": active_triggers,
            "error_triggers": error_triggers,
            "inactive_triggers": inactive_triggers,
            "total_executions": total_executions,
            "avg_execution_time": round(avg_execution_time, 3),
            "generated_at": datetime.utcnow().isoformat(),
        }

    async def cleanup_old_states(self, days: int = 30):
        """
        清理旧的状态记录

        Args:
            days: 保留天数
        """
        cutoff_time = datetime.utcnow() - timedelta(days=days)

        to_remove = []
        for trigger_id, state in self._trigger_states.items():
            last_update = state.get("updated_at")
            if last_update and last_update < cutoff_time:
                to_remove.append(trigger_id)

        for trigger_id in to_remove:
            del self._trigger_states[trigger_id]

        logger.info(f"清理了 {len(to_remove)} 个旧的触发器状态记录")

    async def reset_trigger_state(self, trigger_id: str):
        """
        重置触发器状态

        Args:
            trigger_id: 触发器ID
        """
        if trigger_id in self._trigger_states:
            self._trigger_states[trigger_id] = {
                "trigger_id": trigger_id,
                "state": LifecycleStatus.INACTIVE.value,
                "execution_count": 0,
                "updated_at": datetime.utcnow(),
                "metadata": {},
            }

            logger.info(f"触发器状态已重置: {trigger_id}")
        else:
            logger.warning(f"触发器状态不存在，无法重置: {trigger_id}")

    async def enable_trigger(self, trigger_id: str):
        """
        启用触发器

        Args:
            trigger_id: 触发器ID
        """
        await self.update_trigger_state(trigger_id=trigger_id, state=LifecycleStatus.ACTIVE)
        logger.info(f"触发器已启用: {trigger_id}")

    async def disable_trigger(self, trigger_id: str):
        """
        禁用触发器

        Args:
            trigger_id: 触发器ID
        """
        await self.update_trigger_state(trigger_id=trigger_id, state=LifecycleStatus.DISABLED)
        logger.info(f"触发器已禁用: {trigger_id}")

    def get_memory_usage(self) -> dict[str, Any]:
        """
        获取内存使用情况

        Returns:
            内存使用统计
        """
        import sys  # noqa: PLC0415

        total_states = len(self._trigger_states)
        memory_size = sys.getsizeof(self._trigger_states)

        if total_states > 0:
            avg_state_size = sum(sys.getsizeof(state) for state in self._trigger_states.values()) / total_states
        else:
            avg_state_size = 0

        return {
            "total_states": total_states,
            "memory_size_bytes": memory_size,
            "avg_state_size_bytes": round(avg_state_size, 2),
            "memory_size_mb": round(memory_size / 1024 / 1024, 3),
        }
