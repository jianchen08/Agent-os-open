"""通知与事件处理 Mixin。

负责任务状态变更通知、子任务完成通知、终态生命周期处理、
以及挂起管道的唤醒通知。

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"completed", "failed"})


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
            evt = self._terminal_events.get(task_id)
            if evt is not None:
                evt.set()
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

        # BUG-FIX-fix_20260512_task_status_realtime:
        # 问题根因: task_state_changed 事件仅在后端 EventBus 内部流转，
        #   从未被转发到 WebSocket，前端无法实时感知任务状态变更。
        # 修复方案: 在状态变更时通过 connection_manager 广播
        #   task_status_update 事件到所有活跃的 WebSocket 连接。
        try:
            _task_obj = data.get("task")
            if not _task_obj and self._task_service:
                try:
                    _task_obj = self._task_service.get_task(task_id)
                except Exception:
                    pass
            _ws_payload = {
                "type": "task_status_update",
                "data": {
                    "task_id": task_id,
                    "old_status": data.get("old_status", ""),
                    "new_status": new_status,
                },
            }
            _ws_notifier = self._services.get("ws_interaction_notifier")
            if _ws_notifier:
                _parent_pid = getattr(_task_obj, "parent_pipeline_id", "") or ""
                _ws_tid = ""
                if _parent_pid:
                    from pipeline.registry import get_engine_registry
                    _ws_tid = get_engine_registry().get_thread_id(_parent_pid)
                if _ws_tid and hasattr(_ws_notifier, "send_to_thread"):
                    await _ws_notifier.send_to_thread(_ws_tid, _ws_payload)
                elif hasattr(_ws_notifier, "send_to_user"):
                    _task_obj = _task_obj or (self._task_service.get_task(task_id) if self._task_service else None)
                    _uid = getattr(_task_obj, "user_id", "") or ""
                    if _uid:
                        await _ws_notifier.send_to_user(_uid, _ws_payload)
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
        """终态时触发 worktree 合并/清理（仅 worktree 模式）。

        BUG-FIX-fix_20260513_merge_verify:
        合并失败时不再仅 log warning，而是将任务标记为 failed，
        避免任务显示 completed 但实际文件丢失。
        """
        lifecycle = self._services.get("workspace_lifecycle_manager")
        if not lifecycle:
            return

        lifecycle.restore_ws_meta(task_id)
        ws_meta = lifecycle._ws_meta_store.get(task_id)
        if not ws_meta:
            return

        # 仅 worktree 模式需要合并+清理，plain/shared 模式无 worktree
        if ws_meta.get("mode") != "worktree":
            return

        workspace = ws_meta.get("path", "")
        if not workspace:
            return

        try:
            if new_status == "completed":
                result = lifecycle.on_eval_passed(task_id, workspace, ws_meta)
                if result.get("success"):
                    logger.info("TaskWorker: worktree 合并+清理成功: task_id=%s", task_id)
                else:
                    error_msg = (
                        f"worktree 合并失败，文件未成功同步到项目空间: "
                        f"{result.get('error', 'unknown')}"
                    )
                    if result.get("verify_error"):
                        error_msg += f" | 验证详情: {result['verify_error']}"
                    logger.error(
                        "TaskWorker: %s: task_id=%s, worktree 保留在: %s",
                        error_msg, task_id, workspace,
                    )
                    task_service = self._task_service
                    if task_service:
                        try:
                            await task_service.fail_task(task_id, error_msg)
                        except Exception as fail_err:
                            logger.warning(
                                "TaskWorker: fail_task 也失败: task_id=%s, error=%s",
                                task_id, fail_err,
                            )
            elif new_status == "failed":
                lifecycle.on_eval_failed(task_id, workspace, ws_meta)
                logger.info("TaskWorker: worktree 评估失败处理完成: task_id=%s", task_id)
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

        if not parent_pipeline_id:
            logger.warning(
                "TaskWorker: parent_pipeline_id 为空，无法通知父管道（旧任务不再支持扫描模式）: "
                "task=%s, status=%s",
                task_id, new_status,
            )
            return

        # ── 3. 查找父任务的 task_id，用于 revive 路径恢复正确的 agent_config ──
        parent_task_id_for_revive = ""
        if task_obj:
            parent_task_id_for_revive = getattr(task_obj, "parent_task_id", "") or ""

        # ── 4. 通过统一消息总线注入通知（唯一通知链路） ──
        result = await send_pipeline_message(
            parent_pipeline_id, notification,
            task_id=parent_task_id_for_revive,
        )
        if result.success:
            logger.info(
                "TaskWorker: 通知已注入: pipeline=%s, task=%s, status=%s, method=%s",
                parent_pipeline_id, task_id, new_status, result.method,
            )

            # ── 5. 通过 WebSocket 直接发送系统通知气泡到前端 ──
            # 通知文本已通过 send_pipeline_message 注入引擎供 LLM 处理，
            # 但前端还需要一个独立的消息气泡来渲染系统通知（如任务完成/失败）。
            # 由于管道挂起后 drain_loop 已结束，无法通过 bridge 传递，
            # 因此直接通过 ws_interaction_notifier 发送 system_notification 事件。
            try:
                _ws_notifier = self._services.get("ws_interaction_notifier")
                if _ws_notifier and parent_pipeline_id:
                    from pipeline.registry import get_engine_registry
                    _thread_id = get_engine_registry().get_thread_id(parent_pipeline_id)
                    _level = "info"
                    if new_status != "completed":
                        _level = "warning"
                    _sys_payload = {
                        "type": "system_notification",
                        "data": {
                            "content": notification,
                            "level": _level,
                            "notificationType": "task_notification",
                            "pipeline_id": parent_pipeline_id,
                        },
                    }
                    if _thread_id and hasattr(_ws_notifier, "send_to_thread"):
                        await _ws_notifier.send_to_thread(_thread_id, _sys_payload)
                        logger.info(
                            "TaskWorker: system_notification 已发送: pipeline=%s, thread=%s, status=%s",
                            parent_pipeline_id[:12], _thread_id[:12], new_status,
                        )
                    else:
                        _uid = getattr(task_obj, "user_id", "") or "" if task_obj else ""
                        if _uid and hasattr(_ws_notifier, "send_to_user"):
                            await _ws_notifier.send_to_user(_uid, _sys_payload)
                            logger.info(
                                "TaskWorker: system_notification 已广播(user): task=%s, status=%s",
                                task_id, new_status,
                            )
            except Exception as _ws_exc:
                logger.warning(
                    "TaskWorker: system_notification 发送失败(不影响通知注入): error=%s",
                    _ws_exc,
                )

            if parent_task_id_for_revive:
                wake_evt = self._wake_events.get(parent_task_id_for_revive)
                if wake_evt is not None:
                    wake_evt.set()
                    logger.info(
                        "TaskWorker: wake_evt set for parent task %s (single notification path)",
                        parent_task_id_for_revive,
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
            _ws_notifier = self._services.get("ws_interaction_notifier")
            if not _ws_notifier or not target_id:
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

            _ws_tid = ""
            if _parent_pipeline_id_ws:
                from pipeline.registry import get_engine_registry
                _ws_tid = get_engine_registry().get_thread_id(
                    _parent_pipeline_id_ws,
                )

            if _ws_tid and hasattr(_ws_notifier, "send_to_thread"):
                await _ws_notifier.send_to_thread(_ws_tid, _ws_event_data)
            else:
                logger.warning(
                    "TaskWorker: sub_agent_created 无法路由: "
                    "parent_pipeline=%s thread_id=%s",
                    (
                        _parent_pipeline_id_ws[:12]
                        if _parent_pipeline_id_ws
                        else "(empty)"
                    ),
                    _ws_tid[:12] if _ws_tid else "(empty)",
                )

            logger.info(
                "TaskWorker: sub_agent_created 事件已发送: "
                "task_id=%s, agent=%s, pipeline=%s",
                task_id, target_id, pipeline_id,
            )
        except Exception as _ws_err:
            logger.warning(
                "TaskWorker: 发送 sub_agent_created 事件失败: "
                "task_id=%s, error=%s",
                task_id, _ws_err,
            )
