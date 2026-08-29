"""任务资源清理 Mixin — 工作空间清理、级联删除与容器管理。

从 service.py 拆分出的职责域，提供 TaskService 的所有资源清理方法。
依赖 _TaskCrudMixin 和 _TaskStateMixin 的基础方法。

跨进程能力（pipeline-executor 停/删管道、frontend.emit 前端通知）经
set_cleanup_capabilities 注入（server.py on_load）；未注入时降级留痕，
不阻断删除主流程。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
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
            from isolation.manager import get_isolation_manager  # noqa: PLC0415

            manager = await get_isolation_manager()
            await manager.destroy_by_task_id(task_id)
            cleanup_results["container_destroyed"] = True
            logger.info("[TaskService] 已通过 IsolationManager 销毁环境: %s", task_id)
        except Exception as e:
            cleanup_results["errors"].append(f"清理隔离环境失败: {str(e)}")
            logger.warning("[TaskService] 清理隔离环境失败: %s, 错误: %s", task_id, e)

        # 工作空间清理直接走下方路径删除（workspace_lifecycle 插件语义由
        # 管道输入步承载；此处纯文件面：目录/worktree + .git 分支安全删除）
        if workspace:
            try:
                from isolation.workspace import get_workspace_config_root  # noqa: PLC0415

                workspace_path = Path(workspace)
                ws_root = get_workspace_config_root()

                if not workspace_path.is_absolute():
                    workspace_path = Path(ws_root) / workspace

                ws_root_resolved = Path(ws_root).resolve()
                ws_path_resolved = workspace_path.resolve()

                if not ws_path_resolved.is_relative_to(ws_root_resolved):
                    logger.warning(
                        "[TaskService] 拒绝删除工作空间（不在配置根目录下）: %s (root=%s)",
                        ws_path_resolved,
                        ws_root_resolved,
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
                    logger.debug("[TaskService] 工作空间不存在: %s", workspace)
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

        除了 `git worktree remove`，还要删除 worktree 关联的 task 分支，否则任务
        取消/失败走本路径清理时 worktree 目录删了但分支永久残留，导致 task/* 分支
        随任务无限堆积。
        流程：remove 前用 `git -C <workspace> rev-parse --abbrev-ref HEAD` 反查
        worktree 当前分支名（detached 时为空则跳过），remove 成功后补
        `git branch -D` 删除。反查在 remove 之前，因为删后工作区就没了。

        Args:
            workspace_path: worktree 的工作空间路径
            cleanup_results: 清理结果字典，用于记录错误信息
        """
        try:
            git_file_content = (workspace_path / ".git").read_text(encoding="utf-8").strip()
            if git_file_content.startswith("gitdir: "):
                worktree_gitdir = Path(git_file_content[len("gitdir: ") :])
                main_repo = worktree_gitdir.parent.parent.parent
            else:
                main_repo = workspace_path.parent

            # remove 前反查分支名：detach 状态下返回 HEAD，此时无分支可删，跳过
            branch_to_delete = ""
            branch_probe = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if branch_probe.returncode == 0:
                branch_to_delete = branch_probe.stdout.strip()
            if not branch_to_delete or branch_to_delete == "HEAD":
                branch_to_delete = ""

            subprocess.run(
                ["git", "worktree", "remove", str(workspace_path), "--force"],
                cwd=str(main_repo),
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info("[TaskService] 已通过 git worktree remove 清理 worktree: %s", workspace_path)
            cleanup_results["workspace_cleaned"] = True

            # 删除 worktree 关联分支，止住 task/* 僵尸分支堆积
            if branch_to_delete:
                branch_del = subprocess.run(
                    ["git", "branch", "-D", branch_to_delete],
                    cwd=str(main_repo),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if branch_del.returncode == 0:
                    logger.info(
                        "[TaskService] 已删除 worktree 关联分支: %s (源: %s)",
                        branch_to_delete,
                        workspace_path,
                    )
                else:
                    cleanup_results["errors"].append(
                        f"删除分支失败: {branch_to_delete} — {branch_del.stderr.strip() or 'unknown'}"
                    )
                    logger.warning(
                        "[TaskService] 删除分支失败: %s, stderr: %s",
                        branch_to_delete,
                        branch_del.stderr,
                    )
        except subprocess.CalledProcessError as e:
            cleanup_results["errors"].append(f"git worktree remove 失败: {e.stderr.strip() if e.stderr else str(e)}")
            logger.warning(
                "[TaskService] git worktree remove 失败: %s, stderr: %s",
                workspace_path,
                e.stderr,
            )
        except Exception as e:
            cleanup_results["errors"].append(f"清理 worktree 失败: {str(e)}")
            logger.warning("[TaskService] 清理 worktree 失败: %s, 错误: %s", workspace_path, e)

    async def _cleanup_subtask_worktrees(  # noqa: PLR0912,PLR0915
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
            container_task.id,
            len(subtasks),
        )

        for subtask in subtasks:
            workspace = (subtask.metadata or {}).get("workspace", "")

            if not workspace:
                logger.debug(
                    "[TaskService] 子任务 %s 无 workspace_path，跳过",
                    subtask.id,
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
                    subtask.id,
                    workspace,
                )
                result["skipped_count"] += 1
                continue

            try:
                cleanup_result = await self._cleanup_task_resources(
                    task_id=subtask.id,
                    workspace=workspace,
                )
                if cleanup_result.get("workspace_cleaned"):
                    result["cleaned_count"] += 1
                else:
                    errors = cleanup_result.get("errors", [])
                    if errors:
                        result["error_count"] += 1
                        result["errors"].extend([f"子任务 {subtask.id}: {e}" for e in errors])
                    else:
                        result["skipped_count"] += 1

            except Exception as e:
                result["error_count"] += 1
                result["errors"].append(f"子任务 {subtask.id}: {str(e)}")
                logger.warning(
                    "[TaskService] 清理子任务 %s 的 worktree 失败: %s, 错误: %s",
                    subtask.id,
                    workspace,
                    e,
                )

        logger.info(
            "[TaskService] 容器 %s 子任务 worktree 清理完成: 总计=%d, 已清理=%d, 跳过=%d, 失败=%d",
            container_task.id,
            result["total_subtasks"],
            result["cleaned_count"],
            result["skipped_count"],
            result["error_count"],
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

    async def _cascade_cleanup_subtasks(  # noqa: PLR0912
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
            task_id,
            len(descendant_ids),
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
            if descendant_task.pipeline_run_id and await self._cleanup_pipeline_file(descendant_task.pipeline_run_id):
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
                                task_id=descendant_id,
                                workspace=workspace,
                            )
                            if cleanup_result.get("workspace_cleaned"):
                                stats["workspaces_cleaned"] += 1
                        except Exception as e:
                            stats["errors"].append(f"子任务 {descendant_id} 工作空间清理失败: {str(e)}")

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
            "[TaskService] 级联清理完成: 子任务删除=%d, 管道文件清理=%d, 工作空间清理=%d, 错误=%d",
            stats["subtasks_deleted"],
            stats["pipeline_files_cleaned"],
            stats["workspaces_cleaned"],
            len(stats["errors"]),
        )

        return stats

    async def hard_delete_task(  # noqa: PLR0912
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
            "workspaces_cleaned": 0,
            "errors": [],
        }
        subtasks = self.list_subtasks(task_id)
        if subtasks:
            cascade_stats = await self._cascade_cleanup_subtasks(
                task_id,
                skip_workspace=False,
                container_workspace="",
            )

        pipeline_cleaned = False
        if task.pipeline_run_id:
            pipeline_cleaned = await self._cleanup_pipeline_file(task.pipeline_run_id)

        workspace = task.metadata.get("workspace")
        cleanup_results = await self._cleanup_task_resources(
            task_id=task_id,
            workspace=workspace,
        )

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
