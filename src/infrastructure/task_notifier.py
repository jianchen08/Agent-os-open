"""通知与事件处理 Mixin。



负责任务状态变更通知、子任务完成通知、终态生命周期处理、

以及挂起管道的唤醒通知。



从 task_worker.py 拆分而出，降低原文件复杂度。

"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


# TaskStatus 枚举仅有 STOPPED/COMPLETED/FAILED（无 cancelled）。
# stopped 需纳入终态：cancel_task/pause_task 都 emit "stopped"，
# 二者都需走终态副作用（terminal_event / _notify_suspended_pipelines）。
# 是否停止引擎由下方 _is_cancel_stopped 进一步区分（pause 保留引擎待 resume）。
_TERMINAL_STATES = frozenset({"completed", "failed", "stopped"})


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

    def _is_cancel_stopped(self, task_id: str) -> bool:
        """判断 stopped 任务是被 cancel（应停引擎）还是 pause（应保留引擎）。

        pause_task 设 metadata["paused_by"]（可恢复，resume_task 会 engine.wake）；
        cancel_task 设 metadata["cancel_reason"]（不可恢复终态）。
        TaskStatus 只有 STOPPED，二者都 emit "stopped"，靠 metadata 区分。

        判定：有 cancel_reason 或无 paused_by → cancel（停引擎）；
        有 paused_by → pause（保留引擎）。

        Args:
            task_id: 任务 ID

        Returns:
            True 表示应停止引擎（cancel 终态），False 表示保留引擎（pause 可恢复）
        """

        try:
            task = self._task_service.get_task(task_id) if self._task_service else None

        except Exception:
            return True  # 查询失败按 cancel 处理，避免漏停导致引擎空转

        if task is None:
            return True

        meta = getattr(task, "metadata", None) or {}

        if meta.get("paused_by"):
            return False  # pause 产生的 stopped，保留引擎待 resume

        return True  # 无 paused_by 视为 cancel，停止引擎

    async def _on_task_state_changed(self, task_id: str, old_status: str, new_status: str) -> None:
        """处理任务状态变更回调，触发对应的 asyncio.Event。



        由 TaskService 通过直接回调触发（替代 EventBus 事件），

        检查是否为终态，如果是则 set 对应的 asyncio.Event。



        Args:

            task_id: 任务 ID

            old_status: 变更前状态

            new_status: 变更后状态

        """

        if new_status in _TERMINAL_STATES:
            logger.info(
                "TaskWorker: 收到终态回调 | task=%s, status=%s",
                task_id,
                new_status,
            )

            ctx = self._contexts.get(task_id)

            if ctx is not None:
                ctx.terminal_event.set()

                logger.debug(
                    "TaskWorker: terminal event set for task %s (%s)",
                    task_id,
                    new_status,
                )

            # 任务进入终态时，终止关联管道引擎。
            # failed → 必停；stopped → 仅 cancel_task 产生的（无 paused_by）才停，
            # pause_task 产生的（有 paused_by）保留引擎待 resume_task 唤醒。
            # 历史根因：TaskStatus 无 cancelled，cancel_task emit "stopped"，
            # 旧代码判 ("cancelled","failed") 永远不匹配 stopped → 引擎空转。
            _should_stop_engine = new_status == "failed" or (
                new_status == "stopped" and self._is_cancel_stopped(task_id)
            )

            if _should_stop_engine:
                try:
                    _cancelled = self.cancel_pipeline(task_id)

                    logger.info(
                        "TaskWorker: 任务终态取消管道 | task=%s, status=%s, cancelled=%s",
                        task_id,
                        new_status,
                        _cancelled,
                    )

                except Exception as _cp_exc:
                    logger.warning(
                        "TaskWorker: 取消管道失败(不影响回调) | task=%s, status=%s, error=%s",
                        task_id,
                        new_status,
                        _cp_exc,
                    )

            elif new_status == "stopped":
                logger.info(
                    "TaskWorker: 任务 stopped 但为 pause（保留引擎待 resume）| task=%s",
                    task_id,
                )

            try:
                await self._check_stale_containers()

            except Exception as exc:
                logger.warning(
                    "TaskWorker: _check_stale_containers 失败(不影响通知): error=%s",
                    exc,
                )

            try:
                await self._notify_suspended_pipelines(task_id, new_status)

            except Exception as exc:
                logger.error(
                    "TaskWorker: _notify_suspended_pipelines 失败: task=%s, status=%s, error=%s",
                    task_id,
                    new_status,
                    exc,
                    exc_info=True,
                )

        try:
            from channels.websocket.ws_handler import ws_interaction_notifier as _notifier  # noqa: PLC0415

            _task_obj = None

            if self._task_service:
                with contextlib.suppress(Exception):
                    _task_obj = self._task_service.get_task(task_id)

            _user_id = ""

            if _task_obj and hasattr(_task_obj, "metadata") and _task_obj.metadata:
                _user_id = _task_obj.metadata.get("user_id", "")

            _task_error = ""

            if _task_obj:
                _task_error = getattr(_task_obj, "error", "") or ""

            if not _user_id:
                logger.error(
                    "TaskWorker: task metadata 缺 user_id，task_status_update 无法按用户投递 | task=%s",
                    task_id,
                )

            elif _notifier:
                await _notifier.send_to_user(
                    _user_id,
                    {
                        "type": "task_status_update",
                        "data": {
                            "task_id": task_id,
                            "old_status": old_status,
                            "new_status": new_status,
                            "current_phase": _STATUS_TO_PHASE.get(new_status, "prepare"),
                            "error": _task_error,
                        },
                    },
                )

            logger.debug(
                "TaskWorker: task_status_update 已广播 | task=%s, %s -> %s",
                task_id,
                old_status,
                new_status,
            )

        except Exception as _ws_exc:
            logger.warning(
                "TaskWorker: task_status_update 广播失败: task=%s, error=%s",
                task_id,
                _ws_exc,
            )

    async def _handle_terminal_lifecycle(self, task_id: str, new_status: str) -> None:
        """已废弃：安全网机制已移除。worktree 合并由 task_evaluate._complete_task 负责。"""

    async def _notify_suspended_pipelines(self, task_id: str, new_status: str) -> None:  # noqa: PLR0912,PLR0915
        """子任务到达终态时，通过统一消息总线通知父管道。



        parent_pipeline_id 由 task_submit 工具自动注入（来自 ParamInjectPlugin 注入的

        pipeline_id → create_task(parent_pipeline_id=...)），走正常流程的子任务一定有此字段。

        """

        from pipeline.message_bus import send_pipeline_message  # noqa: PLC0415

        logger.info(
            "TaskWorker: _notify_suspended_pipelines 开始 | task=%s, status=%s",
            task_id,
            new_status,
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
            task_id,
            parent_pipeline_id[:12] if parent_pipeline_id else "(none)",
            task_obj is not None,
        )

        if not parent_pipeline_id:
            # parent_pipeline_id 为空说明注入链路断裂，记录系统级错误

            _err_msg = (
                f"系统错误：子任务 {task_id} 缺少 parent_pipeline_id，无法通知父管道。请检查 pipeline_id 注入链路。"
            )

            logger.error("TaskWorker: %s", _err_msg)

            if task_service and task_obj:
                try:
                    parent_task_id = getattr(task_obj, "parent_task_id", None)

                    if parent_task_id:
                        parent_task = task_service.get_task(parent_task_id)

                        if parent_task:
                            parent_task.error = _err_msg

                            await task_service.save_task(parent_task)

                            logger.info(
                                "TaskWorker: 已写入父任务 error | parent_task=%s",
                                parent_task_id,
                            )

                except Exception as _setexc:
                    logger.error("TaskWorker: 写入父任务 error 失败: %s", _setexc)

            return

        if isinstance(task_obj, dict):
            title = task_obj.get("title", task_id)

            error = task_obj.get("error", "")

        elif task_obj:
            title = getattr(task_obj, "title", task_id)

            error = getattr(task_obj, "error", "") or ""

        else:
            title = task_id

            error = ""

        # 从 task.metadata 读取重试计数，在通知中加入重试状态

        _task_meta = getattr(task_obj, "metadata", None) or {}

        retry_count = _task_meta.get("retry_count", 0) if task_obj else 0

        max_retries = _task_meta.get("max_retries", 6) if task_obj else 6

        # ── 上下文使用率 ──

        # 由 TaskService._inject_context_usage() 在任务完成时写入 metadata，

        # 此处直接从 metadata 读取，零降级。

        context_usage_text = ""

        _cu = _task_meta.get("context_usage") if _task_meta else None

        if _cu and isinstance(_cu, dict):
            _pct = _cu.get("pct", 0)

            _input = _cu.get("input_tokens", 0)

            _cw = _cu.get("context_window", 0)

            if _pct > 0:
                if _pct > 60:
                    context_usage_text = (
                        f"\n📊 上下文使用率: {_pct}% ({_input:,}/{_cw:,} tokens)"
                        f"\n⚠️ 建议优先创建新任务（上下文已超过60%，继续派发可能触发压缩或截断）"
                    )

                else:
                    context_usage_text = (
                        f"\n📊 上下文使用率: {_pct}% ({_input:,}/{_cw:,} tokens)"
                        f"\n✅ 可直接继续向此 Agent 派发任务（上下文充足）"
                    )

        if new_status == "completed":
            # 从 evaluation_history 取最后一次评估 summary 附入通知

            _eval_summary = ""

            _eval_history = _task_meta.get("evaluation_history") if _task_meta else None

            if isinstance(_eval_history, list) and _eval_history:
                _last_eval = _eval_history[-1]

                if isinstance(_last_eval, dict):
                    _eval_summary = (_last_eval.get("summary") or "").strip()

            _conclusion_hint = ""

            if _eval_summary:
                _conclusion_hint = f"\n📋 评估结论: {_eval_summary[:200]}"

            notification = (
                f"[系统通知] 子任务 '{title}' (ID: {task_id}) 已完成 ✅"
                f"{_conclusion_hint}"
                f"{context_usage_text}\n"
                "请查阅子任务产出与评估结论后决定下一步。"
            )

        else:
            err_hint = f": {error[:300]}" if error else ""

            if retry_count > 0 and retry_count >= max_retries:
                notification = (
                    f"[系统通知] 子任务 '{title}' (ID: {task_id}) {new_status} ❌"
                    f" (已达最大重试次数 {retry_count}/{max_retries}){err_hint}"
                    f"{context_usage_text}\n"
                    "请放弃重试，考虑其他方案或标记任务失败。"
                )

            elif retry_count > 0:
                notification = (
                    f"[系统通知] 子任务 '{title}' (ID: {task_id}) {new_status} ❌"
                    f" (已重试 {retry_count}/{max_retries} 次){err_hint}"
                    f"{context_usage_text}\n"
                    "请根据失败情况决定后续操作（重试/替代方案/标记失败）。"
                )

            else:
                notification = (
                    f"[系统通知] 子任务 '{title}' (ID: {task_id}) {new_status} ❌{err_hint}"
                    f"{context_usage_text}\n"
                    "请根据失败情况决定后续操作（重试/替代方案/标记失败）。"
                )

        logger.info(
            "TaskWorker: 通知查找开始: task=%s, status=%s, parent_pipeline=%s, parent_task=%s, retry=%d/%d",
            task_id,
            new_status,
            parent_pipeline_id,
            getattr(task_obj, "parent_task_id", None) if task_obj else None,
            retry_count,
            max_retries,
        )

        # ── 3. 查找父任务的 task_id，用于 revive 路径恢复正确的 agent_config ──

        parent_task_id_for_revive = ""

        if task_obj:
            parent_task_id_for_revive = getattr(task_obj, "parent_task_id", "") or ""

        # ── 4. 通过统一消息总线注入通知（唯一通知链路） ──

        # 系统通知气泡由 send_pipeline_message 内部统一发送，

        # 不需要在此处单独调用 send_frontend_event。

        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

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
            parent_pipeline_id[:12],
            parent_task_id_for_revive[:12] if parent_task_id_for_revive else "(none)",
            len(notification),
        )

        from pipeline.message_types import MessageType, PipelineMessage  # noqa: PLC0415

        _notif_msg = PipelineMessage(
            type=MessageType.CHAT,
            content=notification,
            pipeline_id=parent_pipeline_id,
            metadata={"source": "system"},
        )

        result = await send_pipeline_message(
            _notif_msg,
            task_id=parent_task_id_for_revive,
        )

        logger.info(
            "TaskWorker: send_pipeline_message 返回 | success=%s | method=%s | error=%s | pipeline=%s",
            result.success,
            result.method,
            result.error[:100] if result.error else "",
            result.pipeline_id[:12] if result.pipeline_id else "",
        )

        if result.success:
            logger.info(
                "TaskWorker: 通知已注入: pipeline=%s, task=%s, status=%s, method=%s",
                parent_pipeline_id,
                task_id,
                new_status,
                result.method,
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
                        "TaskWorker: wake_evt 未找到 | parent_task=%s | （可能是非阻塞通知或 wake_evt 尚未注册）",
                        parent_task_id_for_revive[:12],
                    )

        else:
            logger.warning(
                "TaskWorker: 通知注入失败: pipeline=%s, task=%s, status=%s, error=%s",
                parent_pipeline_id,
                task_id,
                new_status,
                result.error,
            )

    async def _find_task_by_pipeline_id(self, pipeline_id: str) -> str | None:
        """通过 pipeline_run_id 查找关联的任务 ID。



        用于子任务仅有 parent_pipeline_id（无 parent_task_id）时，

        回退查找父任务以触发级联。



        查找优先级：

        1. 引擎注册表 tags.task_id（O(1)）

        2. task_service.get_all_tasks() 全量扫描（引擎已注销时回退）



        Args:

            pipeline_id: 要查找的 pipeline_run_id



        Returns:

            匹配的任务 ID，未找到返回 None

        """

        if not pipeline_id:
            return None

        task_service = self._task_service

        if not task_service:
            return None

        import contextlib  # noqa: F401,PLC0415

        # ── 路径1：引擎注册表 tags 直接查 task_id ──

        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            _entry = get_engine_registry().get(pipeline_id)

            if _entry is not None and _entry.tags.get("task_id"):
                return _entry.tags["task_id"]

        except Exception:
            pass

        # ── 路径2：全量扫描（引擎已注销，但 task.pipeline_run_id 仍存在）──

        try:
            for task in task_service.get_all_tasks():
                if getattr(task, "pipeline_run_id", None) == pipeline_id:
                    return task.id

        except Exception:
            logger.warning(
                "TaskWorker: _find_task_by_pipeline_id 失败: pipeline_id=%s",
                pipeline_id,
                exc_info=True,
            )

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
                        _task_for_ws,
                        "parent_task_id",
                        None,
                    )

                    _parent_pipeline_id_ws = getattr(_task_for_ws, "parent_pipeline_id", "") or ""

                    _title_ws = _task_for_ws.title or _title_ws

                    _raw_level = getattr(_task_for_ws, "agent_level", None)

                    if _raw_level:
                        _agent_level_ws = str(_raw_level)

            _ws_event_data = {
                "type": "sub_agent_created",
                "data": {
                    "taskId": task_id,
                    "agentId": target_id or task_id,
                    "agentConfigId": target_id,
                    "pipelineId": pipeline_id,
                    "parentPipelineId": _parent_pipeline_id_ws,
                    "agentName": target_id or "子Agent",
                    "title": _title_ws,
                    "description": task_data.get("description", ""),
                    "parentId": _parent_task_id_ws or "",
                    "status": "running",
                    "agentLevel": _agent_level_ws,
                },
            }

            if _parent_pipeline_id_ws:
                from pipeline.stream_bridge import send_frontend_event  # noqa: PLC0415

                await send_frontend_event(
                    _parent_pipeline_id_ws,
                    _ws_event_data,
                )

                logger.info(
                    "TaskWorker: sub_agent_created 事件已发送: task_id=%s, agent=%s, pipeline=%s",
                    task_id,
                    target_id,
                    pipeline_id,
                )

            else:
                logger.warning(
                    "TaskWorker: sub_agent_created 无法路由: parent_pipeline=%s",
                    "(empty)",
                )

        except Exception as _ws_err:
            logger.warning(
                "TaskWorker: 发送 sub_agent_created 事件失败: task_id=%s, error=%s",
                task_id,
                _ws_err,
            )
