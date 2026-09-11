"""任务资源清理 Mixin — 容器销毁与级联删除。

从 service.py 拆分出的职责域，提供 TaskService 的跨进程清理方法。

产物保全契约（2026-09-05 用户裁定）：**删除链不删工作区**——工作区目录的
唯一合法清理点 = 评估通过门控内的 worktree 合并+清理一体
（shared/worktree_merge.py，产物到达验证通过后才执行）。本 Mixin 只销毁
隔离容器（Docker，非文件面）与执行数据（内核表），不触碰工作区目录。

跨进程能力（pipeline-executor 停/删管道、frontend.emit 前端通知）经
set_cleanup_capabilities 注入（server.py on_load）；未注入时降级留痕，
不阻断删除主流程。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 跨进程能力注入点（server.py on_load 装配）：
# _pipeline_executor: async (params: dict) -> dict（pipeline-executor capability）；
# _frontend_emitter: SDK FrontendEmitter（task_deleted 通知）。
_pipeline_executor: Any = None
_frontend_emitter: Any = None


def set_cleanup_capabilities(executor: Any, emitter: Any) -> None:
    global _pipeline_executor  # noqa: PLW0603
    global _frontend_emitter  # noqa: PLW0603
    _pipeline_executor = executor
    _frontend_emitter = emitter


class _TaskCleanupMixin:
    """任务资源清理 Mixin。"""

    async def _cancel_pipeline(self, task_id: str) -> None:
        """取消任务关联的运行中管道（best-effort）。

        0.2 停 = pipeline-executor.suspend_pipeline（task = pipeline，
        task_id 即 pipeline_id）；executor 未注入或管道已终态时降级留痕。
        """

        if _pipeline_executor is None:
            logger.warning(
                "[TaskService] 任务 %s 管道取消跳过：pipeline-executor 未注入", task_id
            )
            return

        try:
            await _pipeline_executor(
                {"method": "suspend_pipeline", "params": {"pipeline_id": task_id}}
            )
            logger.info("[TaskService] 任务 %s 管道已挂起（取消）", task_id)
        except Exception as e:
            logger.warning(
                "[TaskService] 任务 %s 管道取消失败 (non-fatal): %s",
                task_id,
                e,
            )

    async def _cancel_pipeline_recursive(self, task_id: str) -> None:
        """递归取消任务及其所有子任务的运行中管道。"""
        await self._cancel_pipeline(task_id)
        subtasks = self.list_subtasks(task_id)
        for subtask in subtasks:
            await self._cancel_pipeline_recursive(subtask.id)

    async def _cleanup_task_resources(self, task_id: str) -> dict[str, Any]:
        """销毁任务关联的隔离容器（仅容器面，不删工作区目录）。

        工作区目录不进删除链：其唯一清理点在评估通过门控（合并+清理一体，
        产物到达验证通过后才执行）——防止未合并先清理的产物蒸发。
        """
        cleanup_results: dict[str, Any] = {
            "container_destroyed": False,
            "errors": [],
        }

        try:
            from isolation.manager import get_isolation_manager  # noqa: PLC0415

            manager = await get_isolation_manager()
            await manager.destroy_by_task_id(task_id)
            cleanup_results["container_destroyed"] = True
            logger.info("[TaskService] 已通过 IsolationManager 销毁环境: %s", task_id)
        except Exception as e:
            cleanup_results["errors"].append(f"清理隔离环境失败: {str(e)}")
            logger.warning("[TaskService] 清理隔离环境失败: %s, 错误: %s", task_id, e)

        return cleanup_results

    def _collect_all_descendant_ids(self, task_id: str) -> list[str]:
        """递归收集任务的所有后代任务 ID（不含自身，深度优先）。

        Args:
            task_id: 起始任务 ID

        Returns:
            后代任务 ID 列表（叶子节点在前，根在后）
        """
        descendants: list[str] = []
        subtasks = self.list_subtasks(task_id)
        for subtask in subtasks:
            descendants.extend(self._collect_all_descendant_ids(subtask.id))
            descendants.append(subtask.id)
        return descendants

    async def _cleanup_pipeline_file(self, pipeline_run_id: str) -> bool:
        """删除管道在内核的全部执行数据（best-effort）。

        0.2 执行数据 = runs/traces/messages/state/checkpoints（SQLite 表），
        删除 = pipeline-executor.delete_pipeline（内核级联单一清单）；
        0.1 的 ExecutionRecordStorage/JSON 文件已随 infrastructure 层退役。
        """

        if not pipeline_run_id:
            return False

        if _pipeline_executor is None:
            logger.warning(
                "[TaskService] 管道执行数据删除跳过：pipeline-executor 未注入 | pipeline=%s",
                pipeline_run_id,
            )
            return False

        try:
            await _pipeline_executor(
                {"method": "delete_pipeline", "params": {"pipeline_id": pipeline_run_id}}
            )
            logger.info("[TaskService] 已删除管道执行数据: %s", pipeline_run_id)
            return True
        except Exception as e:
            logger.warning(
                "[TaskService] 管道执行数据删除失败 (non-fatal): %s, 错误: %s",
                pipeline_run_id,
                e,
            )
            return False

    async def _cascade_cleanup_subtasks(self, task_id: str) -> dict[str, Any]:
        """级联清理任务的所有子任务资源并删除存储记录。

        清理面 = 管道执行数据（内核表）+ 隔离容器；工作区目录不在删除链上
        （产物保全契约，见模块 docstring）。

        Args:
            task_id: 父任务 ID

        Returns:
            清理统计信息字典
        """
        stats: dict[str, Any] = {
            "subtasks_deleted": 0,
            "pipeline_files_cleaned": 0,
            "containers_destroyed": 0,
            "errors": [],
        }

        descendant_ids = self._collect_all_descendant_ids(task_id)

        if not descendant_ids:
            return stats

        logger.info(
            "[TaskService] 开始级联清理任务 %s 的 %d 个后代子任务",
            task_id,
            len(descendant_ids),
        )

        for descendant_id in descendant_ids:
            descendant_task = self.get_task(descendant_id)
            if descendant_task is None:
                continue

            # 1. 清理管道执行数据
            if descendant_task.pipeline_run_id and await self._cleanup_pipeline_file(descendant_task.pipeline_run_id):
                stats["pipeline_files_cleaned"] += 1

            # 2. 销毁隔离容器（不删工作区目录）
            try:
                cleanup_result = await self._cleanup_task_resources(descendant_id)
                if cleanup_result.get("container_destroyed"):
                    stats["containers_destroyed"] += 1
            except Exception as e:
                stats["errors"].append(f"子任务 {descendant_id} 容器清理失败: {str(e)}")

            # 3. 删除存储记录
            try:
                await self.hard_delete(descendant_id)
                stats["subtasks_deleted"] += 1
            except Exception as e:
                stats["errors"].append(f"子任务 {descendant_id} 记录删除失败: {str(e)}")
                logger.warning(
                    "[TaskService] 删除子任务记录失败 (non-fatal): %s, 错误: %s",
                    descendant_id,
                    e,
                )

        logger.info(
            "[TaskService] 级联清理完成: 子任务删除=%d, 管道文件清理=%d, 容器销毁=%d, 错误=%d",
            stats["subtasks_deleted"],
            stats["pipeline_files_cleaned"],
            stats["containers_destroyed"],
            len(stats["errors"]),
        )

        return stats

    async def hard_delete_task(
        self, task_id: str, reason: str = "用户请求删除"
    ) -> dict[str, Any]:
        """硬删除非容器任务（级联清理 + 删除记录）。

        Args:
            task_id: 任务 ID
            reason: 删除原因

        Returns:
            操作结果字典
        """
        task = self.get_task(task_id)
        if task is None:
            return {"error": f"任务不存在: {task_id}"}

        old_status = task.status.value
        task_title = task.title

        await self._cancel_pipeline_recursive(task_id)

        cascade_stats: dict[str, Any] = {
            "subtasks_deleted": 0,
            "pipeline_files_cleaned": 0,
            "containers_destroyed": 0,
            "errors": [],
        }
        subtasks = self.list_subtasks(task_id)
        if subtasks:
            cascade_stats = await self._cascade_cleanup_subtasks(task_id)

        pipeline_cleaned = False
        if task.pipeline_run_id:
            pipeline_cleaned = await self._cleanup_pipeline_file(task.pipeline_run_id)

        cleanup_results = await self._cleanup_task_resources(task_id)

        await self.hard_delete(task_id)

        # 前端通知（frontend.emit fire-and-forget；未注入/发送失败静默——
        # 观测出口不阻断删除主流程）。payload 携带 pipeline_id 路由键。
        if _frontend_emitter is not None and _frontend_emitter.available:
            user_id = (task.metadata.get("user_id") if task.metadata else "") or ""
            await _frontend_emitter.emit(
                "task_deleted",
                {
                    "pipeline_id": task_id,
                    "thread_id": task_id,
                    "task_id": task_id,
                    "title": task_title,
                    "user_id": user_id,
                },
            )

        return {
            "task_id": task_id,
            "deleted": True,
            "old_status": old_status,
            "title": task_title,
            "reason": reason,
            "pipeline_file_cleaned": pipeline_cleaned,
            "cleanup": cleanup_results,
            "cascade_cleanup": cascade_stats,
        }
