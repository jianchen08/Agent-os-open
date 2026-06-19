"""任务资源清理 Mixin — 工作空间清理、级联删除与容器管理。

从 service.py 拆分出的职责域，提供 TaskService 的所有资源清理方法。
依赖 _TaskCrudMixin 和 _TaskStateMixin 的基础方法。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _TaskCleanupMixin:
    """任务资源清理 Mixin。"""

    def _get_execution_record_storage(self):
        """获取全局 ExecutionRecordStorage 实例。委托到公共接口。"""
        from infrastructure.service_access import get_execution_record_storage
        return get_execution_record_storage()

    def _cancel_pipeline(self, task_id: str) -> None:
        """取消任务关联的运行中管道（best-effort）。"""
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            task_worker = provider.get("task_worker")
            if task_worker is None:
                return
            cancelled = task_worker.cancel_pipeline(task_id)
            if cancelled:
                logger.info("[TaskService] 任务 %s 管道已取消", task_id)
        except Exception as e:
            logger.warning(
                "[TaskService] 任务 %s 管道取消失败 (non-fatal): %s",
                task_id, e,
            )

    def _cancel_pipeline_recursive(self, task_id: str) -> None:
        """递归取消任务及其所有子任务的运行中管道。"""
        self._cancel_pipeline(task_id)
        subtasks = self.list_subtasks(task_id)
        for subtask in subtasks:
            self._cancel_pipeline_recursive(subtask.id)

    def _is_child_of_container(self, task: Any) -> bool:
        """判断非容器任务是否属于某个容器任务的子树。"""
        root_id = self.get_root_task_id(task.id)
        if root_id is None or root_id == task.id:
            return False
        root_task = self.get_task(root_id)
        if root_task is None:
            return False
        return root_task.metadata.get("task_scope") == "container"

    async def _cleanup_task_resources(
        self,
        task_id: str,
        workspace: str | None,
    ) -> dict[str, Any]:
        """清理任务相关的资源（容器和工作空间）。

        Args:
            task_id: 任务 ID
            workspace: 工作空间路径

        Returns:
            清理结果字典
        """
        cleanup_results: dict[str, Any] = {
            "container_destroyed": False,
            "workspace_cleaned": False,
            "errors": [],
        }

        try:
            from isolation.manager import get_isolation_manager

            manager = await get_isolation_manager()
            destroyed = await manager.destroy_environment(task_id)
            cleanup_results["container_destroyed"] = destroyed
            if destroyed:
                logger.info("[TaskService] 已通过 IsolationManager 销毁环境: %s", task_id)
        except Exception as e:
            cleanup_results["errors"].append(f"清理隔离环境失败: {str(e)}")
            logger.warning("[TaskService] 清理隔离环境失败: %s, 错误: %s", task_id, e)

        lifecycle_cleaned = False
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            lifecycle = provider.get("workspace_lifecycle_manager")
            if lifecycle:
                lifecycle.restore_ws_meta(task_id)
                cleanup_result = lifecycle.cleanup_workspace(task_id)
                if cleanup_result:
                    lifecycle_cleaned = True
                    cleanup_results["workspace_cleaned"] = True
                    logger.info("[TaskService] 已通过 lifecycle 清理工作空间: %s", task_id)
        except Exception as e:
            logger.debug("[TaskService] lifecycle 清理不可用，回退到原有逻辑: %s", e)

        if not lifecycle_cleaned and workspace:
            try:
                from isolation.workspace import get_workspace_config_root

                workspace_path = Path(workspace)
                ws_root = get_workspace_config_root()

                if not workspace_path.is_absolute():
                    workspace_path = Path(ws_root) / workspace

                ws_root_resolved = Path(ws_root).resolve()
                ws_path_resolved = workspace_path.resolve()

                if not ws_path_resolved.is_relative_to(ws_root_resolved):
                    logger.warning(
                        "[TaskService] 拒绝删除工作空间（不在配置根目录下）: %s (root=%s)",
                        ws_path_resolved, ws_root_resolved,
                    )
                    cleanup_results["errors"].append(
                        f"安全拦截：路径 {ws_path_resolved} 不在工作空间根目录 {ws_root_resolved} 下，已跳过删除"
                    )
                elif workspace_path.exists():
                    git_path = workspace_path / ".git"
                    if git_path.is_file():
                        self._remove_worktree(workspace_path, cleanup_results)
                    else:
                        shutil.rmtree(str(workspace_path))
                        cleanup_results["workspace_cleaned"] = True
                        logger.info("[TaskService] 已清理目录: %s", workspace_path)
                else:
                    logger.debug("[TaskService] 工作空间不存在: %s", workspace_path)
            except Exception as e:
                cleanup_results["errors"].append(f"清理工作空间失败: {str(e)}")
                logger.warning("[TaskService] 清理工作空间失败: %s, 错误: %s", workspace, e)

        return cleanup_results

    def _remove_worktree(
        self,
        workspace_path: Path,
        cleanup_results: dict[str, Any],
    ) -> None:
        """移除 git worktree 并清理对应分支。

        Args:
            workspace_path: worktree 的工作空间路径
            cleanup_results: 清理结果字典，用于记录错误信息
        """
        try:
            git_file_content = (workspace_path / ".git").read_text(encoding="utf-8").strip()
            if git_file_content.startswith("gitdir: "):
                worktree_gitdir = Path(git_file_content[len("gitdir: "):])
                main_repo = worktree_gitdir.parent.parent.parent
            else:
                main_repo = workspace_path.parent

            subprocess.run(
                ["git", "worktree", "remove", str(workspace_path), "--force"],
                cwd=str(main_repo),
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info("[TaskService] 已通过 git worktree remove 清理 worktree: %s", workspace_path)
            cleanup_results["workspace_cleaned"] = True
        except subprocess.CalledProcessError as e:
            cleanup_results["errors"].append(
                f"git worktree remove 失败: {e.stderr.strip() if e.stderr else str(e)}"
            )
            logger.warning(
                "[TaskService] git worktree remove 失败: %s, stderr: %s",
                workspace_path, e.stderr,
            )
        except Exception as e:
            cleanup_results["errors"].append(f"清理 worktree 失败: {str(e)}")
            logger.warning("[TaskService] 清理 worktree 失败: %s, 错误: %s", workspace_path, e)

    async def _cleanup_subtask_worktrees(
        self,
        container_task: Any,
        subtasks: list[Any],
    ) -> dict[str, Any]:
        """清理容器下所有子任务的 worktree。

        Args:
            container_task: 容器任务模型
            subtasks: 容器下的子任务列表

        Returns:
            清理结果统计字典
        """
        result: dict[str, Any] = {
            "total_subtasks": len(subtasks),
            "cleaned_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "errors": [],
        }

        if not subtasks:
            logger.info(
                "[TaskService] 容器 %s 无子任务，跳过 worktree 清理",
                container_task.id,
            )
            return result

        container_workspace = (container_task.metadata or {}).get("workspace", "")
        container_ws_resolved = ""
        if container_workspace:
            try:
                container_ws_resolved = str(Path(container_workspace).resolve())
            except Exception:
                container_ws_resolved = container_workspace

        logger.info(
            "[TaskService] 开始清理容器 %s 的子任务 worktree，共 %d 个子任务",
            container_task.id, len(subtasks),
        )

        for subtask in subtasks:
            workspace = (subtask.metadata or {}).get("workspace", "")

            if not workspace:
                logger.debug(
                    "[TaskService] 子任务 %s 无 workspace_path，跳过", subtask.id,
                )
                result["skipped_count"] += 1
                continue

            try:
                sub_ws_resolved = str(Path(workspace).resolve())
            except Exception:
                sub_ws_resolved = workspace

            if container_ws_resolved and sub_ws_resolved == container_ws_resolved:
                logger.info(
                    "[TaskService] 子任务 %s 的 workspace 与容器相同 (%s)，跳过",
                    subtask.id, workspace,
                )
                result["skipped_count"] += 1
                continue

            try:
                lifecycle_cleaned = False
                try:
                    from infrastructure.service_provider import get_service_provider

                    provider = get_service_provider()
                    lifecycle = provider.get("workspace_lifecycle_manager")
                    if lifecycle:
                        lifecycle.restore_ws_meta(subtask.id)
                        lc_result = lifecycle.cleanup_workspace(subtask.id)
                        if lc_result and (
                            lc_result.get("worktree_removed") or lc_result.get("dir_removed")
                        ):
                            lifecycle_cleaned = True
                            result["cleaned_count"] += 1
                            logger.info(
                                "[TaskService] 已通过 lifecycle 清理子任务 %s 的 worktree: %s",
                                subtask.id, workspace,
                            )
                except Exception as e:
                    logger.debug(
                        "[TaskService] lifecycle 清理子任务 %s 不可用: %s",
                        subtask.id, e,
                    )

                if not lifecycle_cleaned:
                    cleanup_result = await self._cleanup_task_resources(
                        task_id=subtask.id, workspace=workspace,
                    )
                    if cleanup_result.get("workspace_cleaned"):
                        result["cleaned_count"] += 1
                    else:
                        errors = cleanup_result.get("errors", [])
                        if errors:
                            result["error_count"] += 1
                            result["errors"].extend(
                                [f"子任务 {subtask.id}: {e}" for e in errors]
                            )
                        else:
                            result["skipped_count"] += 1

            except Exception as e:
                result["error_count"] += 1
                result["errors"].append(f"子任务 {subtask.id}: {str(e)}")
                logger.warning(
                    "[TaskService] 清理子任务 %s 的 worktree 失败: %s, 错误: %s",
                    subtask.id, workspace, e,
                )

        logger.info(
            "[TaskService] 容器 %s 子任务 worktree 清理完成: "
            "总计=%d, 已清理=%d, 跳过=%d, 失败=%d",
            container_task.id,
            result["total_subtasks"], result["cleaned_count"],
            result["skipped_count"], result["error_count"],
        )

        return result

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

    def _cleanup_pipeline_file(self, pipeline_run_id: str) -> bool:
        """清理单个管道的执行记录文件（best-effort）。

        Args:
            pipeline_run_id: 管道运行 ID

        Returns:
            是否成功清理了记录
        """
        if not pipeline_run_id:
            return False
        try:
            storage = self._get_execution_record_storage()
            if storage is None:
                return False
            deleted = storage.delete_by_session(pipeline_run_id)
            if deleted > 0:
                logger.info(
                    "[TaskService] 已清理管道执行文件: %s (%d 条记录)",
                    pipeline_run_id, deleted,
                )
                return True
            return False
        except Exception as e:
            logger.warning(
                "[TaskService] 清理管道执行文件失败 (non-fatal): %s, 错误: %s",
                pipeline_run_id, e,
            )
            return False

    async def _cascade_cleanup_subtasks(
        self,
        task_id: str,
        *,
        skip_workspace: bool = False,
        container_workspace: str = "",
    ) -> dict[str, Any]:
        """级联清理任务的所有子任务资源并删除存储记录。

        Args:
            task_id: 父任务 ID
            skip_workspace: 是否完全跳过工作空间清理
            container_workspace: 容器自身的 workspace 路径

        Returns:
            清理统计信息字典
        """
        stats: dict[str, Any] = {
            "subtasks_deleted": 0,
            "pipeline_files_cleaned": 0,
            "workspaces_cleaned": 0,
            "errors": [],
        }

        descendant_ids = self._collect_all_descendant_ids(task_id)

        if not descendant_ids:
            return stats

        logger.info(
            "[TaskService] 开始级联清理任务 %s 的 %d 个后代子任务",
            task_id, len(descendant_ids),
        )

        container_ws_resolved = ""
        if container_workspace:
            try:
                container_ws_resolved = str(Path(container_workspace).resolve())
            except Exception:
                container_ws_resolved = container_workspace

        for descendant_id in descendant_ids:
            descendant_task = self.get_task(descendant_id)
            if descendant_task is None:
                continue

            # 1. 清理管道执行文件
            if (
                descendant_task.pipeline_run_id
                and self._cleanup_pipeline_file(descendant_task.pipeline_run_id)
            ):
                stats["pipeline_files_cleaned"] += 1

            # 2. 清理工作空间
            if not skip_workspace:
                workspace = (descendant_task.metadata or {}).get("workspace")
                if workspace:
                    try:
                        sub_ws_resolved = str(Path(workspace).resolve())
                    except Exception:
                        sub_ws_resolved = workspace

                    if container_ws_resolved and sub_ws_resolved == container_ws_resolved:
                        logger.debug(
                            "[TaskService] 子任务 %s 的 workspace 与容器相同，跳过",
                            descendant_id,
                        )
                    else:
                        try:
                            cleanup_result = await self._cleanup_task_resources(
                                task_id=descendant_id, workspace=workspace,
                            )
                            if cleanup_result.get("workspace_cleaned"):
                                stats["workspaces_cleaned"] += 1
                        except Exception as e:
                            stats["errors"].append(
                                f"子任务 {descendant_id} 工作空间清理失败: {str(e)}"
                            )

            # 3. 删除存储记录
            try:
                await self.hard_delete(descendant_id)
                stats["subtasks_deleted"] += 1
            except Exception as e:
                stats["errors"].append(
                    f"子任务 {descendant_id} 记录删除失败: {str(e)}"
                )
                logger.warning(
                    "[TaskService] 删除子任务记录失败 (non-fatal): %s, 错误: %s",
                    descendant_id, e,
                )

        logger.info(
            "[TaskService] 级联清理完成: 子任务删除=%d, 管道文件清理=%d, 工作空间清理=%d, 错误=%d",
            stats["subtasks_deleted"], stats["pipeline_files_cleaned"],
            stats["workspaces_cleaned"], len(stats["errors"]),
        )

        return stats

    async def soft_delete_container(
        self, task_id: str, reason: str = "用户请求删除"
    ) -> dict[str, Any]:
        """软删除容器任务（标记取消 + 级联清理子任务）。

        Args:
            task_id: 任务 ID
            reason: 删除原因

        Returns:
            操作结果字典
        """
        from tasks.types import TaskStatus

        task = self.get_task(task_id)
        if task is None:
            return {"error": f"任务不存在: {task_id}"}

        old_status = task.status.value
        task_title = task.title

        task.status = TaskStatus.FAILED
        task.error = f"已取消: {reason}"
        if task.metadata is None:
            task.metadata = {}
        task.metadata["soft_deleted"] = True
        await self.save_task(task)

        self._cancel_pipeline_recursive(task_id)
        cascaded = await self.cancel_task_cascade(task_id, reason=reason)

        container_workspace = (task.metadata or {}).get("workspace", "")
        cascade_stats = await self._cascade_cleanup_subtasks(
            task_id, skip_workspace=False, container_workspace=container_workspace,
        )

        result: dict[str, Any] = {
            "task_id": task_id,
            "deleted": False,
            "soft_deleted": True,
            "old_status": old_status,
            "title": task_title,
            "reason": reason,
            "message": "容器任务已标记删除（软删除）",
            "pipeline_file_cleaned": False,
            "cascade_cleanup": cascade_stats,
        }
        if cascaded > 0:
            result["cascaded_subtasks"] = cascaded
        return result

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
        import contextlib

        task = self.get_task(task_id)
        if task is None:
            return {"error": f"任务不存在: {task_id}"}

        old_status = task.status.value
        task_title = task.title

        is_child_of_container = self._is_child_of_container(task)
        skip_workspace = is_child_of_container

        self._cancel_pipeline_recursive(task_id)

        cascade_stats: dict[str, Any] = {
            "subtasks_deleted": 0,
            "pipeline_files_cleaned": 0,
            "workspaces_cleaned": 0,
            "errors": [],
        }
        subtasks = self.list_subtasks(task_id)
        if subtasks:
            cascade_stats = await self._cascade_cleanup_subtasks(
                task_id, skip_workspace=skip_workspace, container_workspace="",
            )

        pipeline_cleaned = False
        if task.pipeline_run_id:
            pipeline_cleaned = self._cleanup_pipeline_file(task.pipeline_run_id)

        if not skip_workspace:
            workspace = task.metadata.get("workspace")
            cleanup_results = await self._cleanup_task_resources(
                task_id=task_id, workspace=workspace,
            )
        else:
            cleanup_results = {"skipped": "容器子任务不清理工作空间"}

        await self.hard_delete(task_id)

        # WebSocket 通知
        try:
            from infrastructure.service_provider import get_service_provider
            _provider = get_service_provider()
            _ws_notifier = _provider.get("ws_interaction_notifier")
            if _ws_notifier:
                _ws_payload = {
                    "type": "task_deleted",
                    "data": {"task_id": task_id, "title": task_title},
                }
                _parent_pid = getattr(task, "parent_pipeline_id", "") or ""
                _ws_tid = ""
                if _parent_pid and hasattr(_ws_notifier, "get_thread_for_pipeline"):
                    _ws_tid = _ws_notifier.get_thread_for_pipeline(_parent_pid)
                if _ws_tid and hasattr(_ws_notifier, "send_to_thread"):
                    await _ws_notifier.send_to_thread(_ws_tid, _ws_payload)
                    logger.debug(
                        "[TaskService] task_deleted 已通过 send_to_thread 发送 | task_id=%s",
                        task_id,
                    )
                elif hasattr(_ws_notifier, "send_to_user"):
                    _conns = getattr(_ws_notifier, "_active_connections", {})
                    _global_conns = getattr(_ws_notifier, "_global_connections", {})
                    if _conns or _global_conns:
                        for _tid, _ws_list in _conns.items():
                            for _ws in _ws_list:
                                with contextlib.suppress(Exception):
                                    await _ws.send_json(_ws_payload)
                        for _uid, _ws_list in _global_conns.items():
                            for _ws in _ws_list:
                                with contextlib.suppress(Exception):
                                    await _ws.send_json(_ws_payload)
                        logger.debug(
                            "[TaskService] task_deleted 已广播 | task_id=%s", task_id,
                        )
        except Exception as _ws_exc:
            logger.warning("[TaskService] task_deleted 广播失败: %s", _ws_exc)

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
