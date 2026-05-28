"""通知与事件处理 Mixin。

负责任务状态变更通知、子任务完成通知、终态生命周期处理、
以及挂起管道的唤醒通知。

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"completed", "failed"})

_STATUS_TO_PHASE: dict[str, str] = {
    "pending": "prepare",
    "scheduled": "prepare",
    "suspended": "prepare",
    "running": "execute",
    "evaluating": "evaluate",
    "completed": "prepare",
    "failed": "prepare",
    "cancelled": "prepare",
    "timeout": "execute",
}


class TaskNotifierMixin:
    """通知与事件处理混入类。

    提供 _on_task_state_changed、_handle_terminal_lifecycle、
    _notify_suspended_pipelines、_build_child_notifications、
    _find_task_by_pipeline_id、_send_sub_agent_created_event 等方法，
    由 TaskWorker 通过多继承组合使用。
    """

    async def _on_task_state_changed(self, event: Any) -> None:
        """处理任务状态变更事件，触发对应的 asyncio.Event。

        当 TaskService 的 on_state_change 回调 emit 了
        task_state_changed 事件时，检查是否为终态，
        如果是则 set 对应的 asyncio.Event。

        Args:
            event: 状态变更事件
        """
        data = event.data if hasattr(event, "data") else event
        if not isinstance(data, dict):
            return

        task_id = data.get("task_id", "")
        new_status = data.get("new_status", "")

        if new_status in _TERMINAL_STATES:
            logger.info(
                "TaskWorker: 收到终态事件 | task=%s, status=%s, source=%s",
                task_id, new_status, data.get("source", "unknown"),
            )
            ctx = self._contexts.get(task_id)
            if ctx is not None:
                ctx.terminal_event.set()
                logger.debug(
                    "TaskWorker: terminal event set for task %s (%s)",
                    task_id, new_status,
                )

            # BUG-FIX-fix_20260510_notify_guard:
            # 问题根因: _handle_terminal_lifecycle / _check_stale_containers 抛异常时
            #   后续的 _notify_suspended_pipelines 不会被执行，导致上级管道永远收不到通知。
            # 修复方案: 每个调用独立 try-except，确保通知逻辑不受其他钩子影响。
            try:
                await self._handle_terminal_lifecycle(task_id, new_status)
            except Exception as exc:
                logger.warning(
                    "TaskWorker: _handle_terminal_lifecycle 失败(不影响通知): task=%s, error=%s",
                    task_id, exc,
                )

            try:
                await self._check_stale_containers()
            except Exception as exc:
                logger.warning(
                    "TaskWorker: _check_stale_containers 失败(不影响通知): error=%s",
                    exc,
                )

            try:
                await self._notify_suspended_pipelines(task_id, new_status, data)
            except Exception as exc:
                logger.error(
                    "TaskWorker: _notify_suspended_pipelines 失败: task=%s, status=%s, error=%s",
                    task_id, new_status, exc, exc_info=True,
                )

        # BUG-FIX-fix_20260526_status_push:
        # 任务状态变更统一通过 ws_interaction_notifier 推送给用户。
        # 不管是顶层任务还是子任务，状态变了就推。
        try:
            from ws_handler import ws_interaction_notifier as _notifier
            _task_obj = data.get("task")
            if not _task_obj and self._task_service:
                try:
                    _task_obj = self._task_service.get_task(task_id)
                except Exception:
                    pass

            _user_id = ""
            if _task_obj and hasattr(_task_obj, "metadata") and _task_obj.metadata:
                _user_id = _task_obj.metadata.get("user_id", "")

            _task_error = ""
            if _task_obj:
                _task_error = getattr(_task_obj, "error", "") or ""
            elif data.get("error"):
                _task_error = data["error"]

            if _notifier and _user_id:
                await _notifier.send_to_user(_user_id, {
                    "type": "task_status_update",
                    "data": {
                        "task_id": task_id,
                        "old_status": data.get("old_status", ""),
                        "new_status": new_status,
                        "current_phase": _STATUS_TO_PHASE.get(new_status, "prepare"),
                        "error": _task_error,
                    },
                })

            logger.debug(
                "TaskWorker: task_status_update 已广播 | task=%s, %s -> %s",
                task_id, data.get("old_status", ""), new_status,
            )
        except Exception as _ws_exc:
            logger.warning(
                "TaskWorker: task_status_update 广播失败: task=%s, error=%s",
                task_id, _ws_exc,
            )

    async def _handle_terminal_lifecycle(self, task_id: str, new_status: str) -> None:
        """终态时处理 worktree 回滚（仅 worktree 模式的 failed 状态）。

        合并前置策略：worktree 合并已在 task_evaluate._complete_task 中完成，
        此处不再执行合并操作。仅处理 failed 状态的回滚清理。
        """
        lifecycle = self._services.get("workspace_lifecycle_manager")
        if not lifecycle:
            return

        lifecycle.restore_ws_meta(task_id)
        ws_meta = lifecycle._ws_meta_store.get(task_id)
        if not ws_meta:
            return

        if ws_meta.get("mode") != "worktree":
            return

        workspace = ws_meta.get("path", "")
        if not workspace:
            return

        try:
            if new_status == "completed":
                ws_path = Path(workspace).resolve()
                if ws_path.exists():
                    # BUG-FIX-fix_20260528_safetynet_merge_before_cleanup:
                    # 问题根因: 安全网发现 worktree 仍存在时直接 cleanup_workspace 删除，
                    #   不执行合并，导致 agent 的工作成果全部丢失。
                    # 修复方案: 先尝试合并（on_eval_passed），合并成功再清理，
                    #   合并失败则将任务回退为 failed 并保留 worktree。
                    logger.warning(
                        "TaskWorker: 任务已完成但 worktree 仍存在，"
                        "尝试安全网合并: task_id=%s, workspace=%s",
                        task_id, workspace,
                    )
                    merge_result = lifecycle.on_eval_passed(task_id, workspace, ws_meta)
                    if merge_result.get("success"):
                        logger.info(
                            "TaskWorker: 安全网合并成功: task_id=%s, method=%s",
                            task_id, merge_result.get("method"),
                        )
                    else:
                        merge_error = merge_result.get("error", "unknown")
                        logger.error(
                            "TaskWorker: 安全网合并失败，回退任务为 failed: "
                            "task_id=%s, error=%s",
                            task_id, merge_error,
                        )
                        task_service = self._task_service
                        if task_service:
                            try:
                                await task_service.fail_task(
                                    task_id,
                                    f"worktree 合并失败（安全网）: {merge_error}",
                                )
                            except Exception as fail_exc:
                                logger.error(
                                    "TaskWorker: 安全网 fail_task 也失败: "
                                    "task_id=%s, error=%s",
                                    task_id, fail_exc,
                                )
                else:
                    logger.info(
                        "TaskWorker: worktree 已在评估阶段清理: task_id=%s",
                        task_id,
                    )
            elif new_status == "failed":
                ws_path = Path(workspace).resolve()
                if ws_path.exists():
                    logger.warning(
                        "TaskWorker: 任务已失败但 worktree 仍存在，执行安全网清理: "
                        "task_id=%s, workspace=%s",
                        task_id, workspace,
                    )
                    lifecycle.on_task_failed(workspace, ws_meta)
                else:
                    logger.info(
                        "TaskWorker: worktree 已在评估阶段清理: task_id=%s",
                        task_id,
                    )
        except Exception as e:
            logger.warning(
                "TaskWorker: _handle_terminal_lifecycle failed: task_id=%s, error=%s",
                task_id, e,
            )

    async def _notify_suspended_pipelines(self, task_id: str, new_status: str, data: dict) -> None:
        """子任务到达终态时，通过统一消息总线通知父管道。"""
        from pipeline.message_bus import send_pipeline_message

        logger.info(
            "TaskWorker: _notify_suspended_pipelines 开始 | task=%s, status=%s",
            task_id, new_status,
        )

        parent_pipeline_id = None
        task_obj = None
        task_service = self._task_service
        if task_service:
            try:
                task_obj = task_service.get_task(task_id)
                if task_obj:
                    parent_pipeline_id = getattr(task_obj, "parent_pipeline_id", None)
            except Exception as exc:
                logger.warning("TaskWorker: 获取任务信息失败: task=%s, error=%s", task_id, exc)

        logger.info(
            "TaskWorker: 通知查找结果 | task=%s, parent_pipeline=%s, has_task_obj=%s",
            task_id, parent_pipeline_id, task_obj is not None,
        )

        if not parent_pipeline_id:
            logger.info("TaskWorker: 无父管道，跳过通知 | task=%s", task_id)
            return

        # ── 2. 构造通知文本（含重试信息） ──
        task_info = data.get("task", {})
        if isinstance(task_info, dict):
            title = task_info.get("title", task_id)
            error = task_info.get("error", "")
        else:
            title = getattr(task_info, "title", task_id)
            error = getattr(task_info, "error", "") or ""

        # BUG-FIX-fix_20260519_pipeline_retry:
        # 问题根因: 通知文本中不包含 retry_count / max_retries 信息，
        #   父管道 AI 无法区分首次失败还是重试失败，也不知道是否应继续重试。
        # 修复方案: 从 task.metadata 读取重试计数，在通知中加入重试状态，
        #   达到最大次数时明确告知 AI 放弃重试。
        _task_meta = getattr(task_obj, "metadata", None) or {}
        retry_count = _task_meta.get("retry_count", 0) if task_obj else 0
        max_retries = _task_meta.get("max_retries", 3) if task_obj else 3

        if new_status == "completed":
            notification = (
                f"[系统通知] 子任务 '{title}' (ID: {task_id}) 已完成 ✅\n"
                "请继续执行后续流程，提交下一个子任务。"
            )
        else:
            err_hint = f": {error[:100]}" if error else ""
            if retry_count > 0 and retry_count >= max_retries:
                notification = (
                    f"[系统通知] 子任务 '{title}' (ID: {task_id}) {new_status} ❌"
                    f" (已达最大重试次数 {retry_count}/{max_retries}){err_hint}\n"
                    "请放弃重试，考虑其他方案或标记任务失败。"
                )
            elif retry_count > 0:
                notification = (
                    f"[系统通知] 子任务 '{title}' (ID: {task_id}) {new_status} ❌"
                    f" (已重试 {retry_count}/{max_retries} 次){err_hint}\n"
                    "请根据失败情况决定后续操作（重试/替代方案/标记失败）。"
                )
            else:
                notification = (
                    f"[系统通知] 子任务 '{title}' (ID: {task_id}) {new_status} ❌{err_hint}\n"
                    "请根据失败情况决定后续操作（重试/替代方案/标记失败）。"
                )

        logger.info(
            "TaskWorker: 通知查找开始: task=%s, status=%s, parent_pipeline=%s, parent_task=%s, retry=%d/%d",
            task_id, new_status, parent_pipeline_id,
            getattr(task_obj, "parent_task_id", None) if task_obj else None,
            retry_count, max_retries,
        )

        # ── 3. 查找父任务的 task_id，用于 revive 路径恢复正确的 agent_config ──
        parent_task_id_for_revive = ""
        if task_obj:
            parent_task_id_for_revive = getattr(task_obj, "parent_task_id", "") or ""

        # ── 4. 通过统一消息总线注入通知（唯一通知链路） ──
        # 系统通知气泡由 send_pipeline_message 内部统一发送，
        # 不需要在此处单独调用 send_frontend_event。
        try:
            from pipeline.registry import get_engine_registry
            _reg = get_engine_registry()
            _entry = _reg.get(parent_pipeline_id)
            logger.info(
                "TaskWorker: 发送通知前检查引擎注册表 | parent_pipeline=%s | entry=%s | engine=%s | suspended=%s",
                parent_pipeline_id[:12],
                "found" if _entry else "NOT_FOUND",
                "yes" if (_entry and _entry.engine) else "no",
                str(getattr(_entry.engine, "is_suspended", "N/A")) if (_entry and _entry.engine) else "N/A",
            )
        except Exception as _reg_exc:
            logger.warning("TaskWorker: 引擎注册表查询失败: %s", _reg_exc)

        logger.info(
            "TaskWorker: 调用 send_pipeline_message | pipeline=%s | parent_task=%s | notification_len=%d",
            parent_pipeline_id[:12], parent_task_id_for_revive[:12] if parent_task_id_for_revive else "(none)",
            len(notification),
        )
        result = await send_pipeline_message(
            parent_pipeline_id, notification,
            task_id=parent_task_id_for_revive,
            metadata={"source": "system"},
        )
        logger.info(
            "TaskWorker: send_pipeline_message 返回 | success=%s | method=%s | error=%s | pipeline=%s",
            result.success, result.method, result.error[:100] if result.error else "",
            result.pipeline_id[:12] if result.pipeline_id else "",
        )
        if result.success:
            logger.info(
                "TaskWorker: 通知已注入: pipeline=%s, task=%s, status=%s, method=%s",
                parent_pipeline_id, task_id, new_status, result.method,
            )

            if parent_task_id_for_revive:
                parent_ctx = self._contexts.get(parent_task_id_for_revive)
                if parent_ctx is not None:
                    parent_ctx.wake_event.set()
                    logger.info(
                        "TaskWorker: wake_evt set for parent task %s (single notification path)",
                        parent_task_id_for_revive,
                    )
                else:
                    logger.info(
                        "TaskWorker: wake_evt 未找到 | parent_task=%s | "
                        "（可能是非阻塞通知或 wake_evt 尚未注册）",
                        parent_task_id_for_revive[:12],
                    )
        else:
            logger.warning(
                "TaskWorker: 通知注入失败: pipeline=%s, task=%s, status=%s, error=%s",
                parent_pipeline_id, task_id, new_status, result.error,
            )

    # BUG-FIX-fix_20260512_async_list_all: 改为 async def，添加 await
    async def _find_task_by_pipeline_id(self, pipeline_id: str) -> str | None:
        """通过 pipeline_run_id 查找关联的任务 ID。

        用于子任务仅有 parent_pipeline_id（无 parent_task_id）时，
        回退查找父任务以触发级联。

        Args:
            pipeline_id: 要查找的 pipeline_run_id

        Returns:
            匹配的任务 ID，未找到返回 None
        """
        task_service = self._task_service
        if not task_service:
            return None
        try:
            for task in await task_service.list_all(limit=200):
                if getattr(task, "pipeline_run_id", None) == pipeline_id:
                    return task.id
        except Exception:
            logger.warning("TaskWorker: _find_task_by_pipeline_id 失败: pipeline_id=%s", pipeline_id, exc_info=True)
        return None

    def _build_child_notifications(self, parent_task_id: str, task_service: Any) -> str:
        """构建子任务完成通知文本，供 resume 后注入到管道 user_input。

        查找 parent_task_id 下的所有子任务，将最近到达终态的任务
        构建为系统通知文本。

        Args:
            parent_task_id: 父任务 ID
            task_service: 任务服务实例

        Returns:
            通知文本，无子任务时返回空字符串
        """
        if not task_service:
            return ""

        try:
            children = task_service.list_subtasks(parent_task_id)
            if not children:
                return ""
        except Exception:
            return ""

        notifications = []
        for child in children:
            cid = child.id if hasattr(child, "id") else ""
            status_val = child.status.value if hasattr(child.status, "value") else str(child.status)
            title = child.title if hasattr(child, "title") else "未知任务"
            error = getattr(child, "error", "") or ""

            if status_val == "completed":
                notifications.append(f"[系统通知] 子任务 '{title}' (ID: {cid}) 已完成 ✅")
            elif status_val == "failed":
                err_hint = f": {error[:100]}" if error else ""
                notifications.append(f"[系统通知] 子任务 '{title}' (ID: {cid}) 失败 ❌{err_hint}")

        return "\n".join(notifications)

    # ───────────────────────────────────────────────────────────────────
    # 子任务创建通知
    # ───────────────────────────────────────────────────────────────────

    async def _send_sub_agent_created_event(
        self,
        task_id: str,
        target_id: str,
        pipeline_id: str,
        task_data: dict[str, Any],
    ) -> None:
        """子任务启动时通过 WebSocket 通知前端创建子标签。

        Args:
            task_id: 子任务 ID
            target_id: 目标 Agent ID
            pipeline_id: 子管道的 pipeline_run_id
            task_data: 任务提交事件数据
        """
        try:
            if not target_id:
                return

            task_service = self._task_service
            _parent_task_id_ws = None
            _parent_pipeline_id_ws = ""
            _title_ws = task_data.get("user_input", "")
            _agent_level_ws = "L2"

            if task_service:
                _task_for_ws = task_service.get_task(task_id)
                if _task_for_ws:
                    _parent_task_id_ws = getattr(
                        _task_for_ws, "parent_task_id", None,
                    )
                    _parent_pipeline_id_ws = (
                        getattr(_task_for_ws, "parent_pipeline_id", "")
                        or ""
                    )
                    _title_ws = _task_for_ws.title or _title_ws
                    _raw_level = getattr(_task_for_ws, "agent_level", None)
                    if _raw_level:
                        _agent_level_ws = str(_raw_level)

            _ws_event_data = {
                "type": "sub_agent_created",
                "data": {
                    "taskId": task_id,
                    "agentId": task_id,
                    "pipelineId": pipeline_id,
                    "agentName": target_id or "子Agent",
                    "title": _title_ws,
                    "parentId": _parent_task_id_ws or "",
                    "status": "running",
                    "agentLevel": _agent_level_ws,
                },
            }

            if _parent_pipeline_id_ws:
                from pipeline.stream_bridge import send_frontend_event
                await send_frontend_event(
                    _parent_pipeline_id_ws,
                    _ws_event_data,
                )
                logger.info(
                    "TaskWorker: sub_agent_created 事件已发送: "
                    "task_id=%s, agent=%s, pipeline=%s",
                    task_id, target_id, pipeline_id,
                )
            else:
                logger.warning(
                    "TaskWorker: sub_agent_created 无法路由: "
                    "parent_pipeline=%s",
                    "(empty)",
                )
        except Exception as _ws_err:
            logger.warning(
                "TaskWorker: 发送 sub_agent_created 事件失败: "
                "task_id=%s, error=%s",
                task_id, _ws_err,
            )
