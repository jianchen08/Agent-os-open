"""任务管理工具 — 对任务进行生命周期操作。

操作列表：
- get: 获取单个任务详情
- list: 按状态列出任务
- status: 获取任务状态
- pause: 暂停任务（running → paused）
- resume: 恢复任务（paused → running）
- cancel: 取消任务（→ failed）
- retry: 重试失败任务（failed → running）
- reactivate: 重新激活已完成任务（completed → running），携带追加需求
- inject: 向任务会话注入消息
- complete_container: 容器完成（仅限 L1 主 Agent）
- fail_container: 容器失败（仅限 L1 主 Agent）

通过 ctx.get_service("task_service") 获取 TaskService，
通过 ctx.get_service("message_queue") 获取 MessageQueue（inject 操作需要）。

简化原则：
- 去掉 delete 操作
- 禁止普通任务设为 completed（完成只能通过 task_evaluate）
- 容器任务的 completed/failed 通过 complete_container/fail_container 操作

暴露接口：
- task_manage_schema: 工具参数 JSON Schema
- task_manage_func: 工具执行函数
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# BUG-FIX-P9：TaskService 单例缓存，避免每次调用新建实例
_task_service_instance: Any = None


def _get_task_service() -> Any:
    """获取共享的 TaskService 实例（单例）。

    获取优先级：
    1. 模块级缓存实例
    2. ServiceProvider 中已注册的 task_service
    3. 创建新实例（降级兜底）

    Returns:
        TaskService 实例，失败时返回 None
    """
    global _task_service_instance
    if _task_service_instance is not None:
        return _task_service_instance
    try:
        from infrastructure.service_provider import get_service_provider
        svc = get_service_provider().get("task_service")
        if svc is not None:
            _task_service_instance = svc
            return _task_service_instance
        from tasks.service import TaskService
        _task_service_instance = TaskService()
        return _task_service_instance
    except Exception as exc:
        logger.error("[task_manage] TaskService 创建失败: %s", exc)
        return None


def _retry_emit_event(task_id: str) -> None:
    """为 retry 操作发布 task.submitted 事件，触发 TaskWorker 重新执行。

    BUG-FIX-P7：retry 操作将任务状态改为 running 后，需要通知 TaskWorker
    重新启动管道。否则任务永远卡在 running 无人执行。

    Args:
        task_id: 要重新执行的任务 ID
    """
    import asyncio
    import sys

    from infrastructure.service_provider import get_service_provider
    event_bus = get_service_provider().get("event_bus")
    if event_bus is None:
        logger.warning("[task_manage] retry: EventBus 不可用，任务 %s 可能需要手动恢复", task_id)
        return

    # 获取任务详情用于重建事件数据
    task_service = _get_task_service()
    task = task_service.get_task(task_id) if task_service else None
    metadata = getattr(task, "metadata", {}) or {} if task else {}

    event_data = {
        "task_id": task_id,
        "target_type": "agent",
        "target_id": metadata.get("target_id", ""),
        "user_input": task.title if task else "",
        "description": task.description if task else "",
        "acceptance_criteria": metadata.get("acceptance_criteria", {}),
        "workspace": metadata.get("workspace", ""),
        "priority": task.priority.value if task and hasattr(task.priority, "value") else 5,
    }

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(event_bus.emit("task.submitted", event_data))
        else:
            loop.run_until_complete(event_bus.emit("task.submitted", event_data))
        logger.info("[task_manage] retry: task.submitted 已发布 | task_id=%s", task_id)
    except RuntimeError:
        try:
            asyncio.run(event_bus.emit("task.submitted", event_data))
            logger.info("[task_manage] retry: task.submitted 已发布（asyncio.run）| task_id=%s", task_id)
        except Exception as e:
            logger.warning("[task_manage] retry: 事件发布失败 %s: %s", task_id, e)
    except Exception as e:
        logger.warning("[task_manage] retry: 事件发布失败 %s: %s", task_id, e)



# 工具参数 Schema（OpenAI Function Calling 格式）
task_manage_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get", "list", "status", "pause", "resume", "cancel", "retry", "reactivate", "inject", "complete_container", "fail_container"],
            "description": (
                "操作类型。"
                "retry: 重试失败任务。判断原则——方向对用retry（保留工作空间+对话历史，agent继续改旧代码），"
                "方向错用新task_submit（全新agent重新来）。按以下策略处理，禁止跳步："
                "①查看失败原因，判断是方向问题还是执行问题→"
                "②方向对+瞬态错误：retry（工作空间和代码不变，重新跑）→"
                "③方向对+需纠正：retry+message（传递修正方向，agent能看到之前的代码和错误）→"
                "④方向对+验收过严：retry+drop_metrics→"
                "⑤方向错或多次retry仍失败：task_submit重新提交（全新agent，无历史包袱）。"
                "需要项目上下文（如修改现有项目）加 inherit_workspace_from='失败任务ID'；"
                "简单任务或新建项目不加，用空工作空间即可→"
                "⑥多次失败后通知人类。"
                "drop_metrics 可移除过于严格或不适用的评估指标，降低重试难度。"
                "reactivate: 重新激活已完成任务（保留原工作空间），携带追加需求在同一任务上下文中继续。"
                "与 retry 的区别：retry 针对失败任务重新执行，reactivate 针对成功任务追加工作。"
                "inject: 向运行中的任务注入消息。"
                "complete_container/fail_container: 容器操作，仅主Agent可用。"
            ),
        },
        "task_id": {
            "type": "string",
            "description": "任务 ID（get/status/pause/resume/cancel/retry/reactivate/inject 操作必填）",
        },
        "status_filter": {
            "type": "string",
            "enum": ["pending", "running", "evaluating", "completed", "failed", "paused"],
            "description": "按状态过滤（list 操作时使用）",
        },
        "reason": {
            "type": "string",
            "description": "操作原因（cancel 操作时建议填写）",
        },
        "message": {
            "type": "string",
            "description": (
                "消息内容。"
                "inject 操作必填。"
                "retry 操作可选：携带补充信息，新管道首次迭代时自动注入，用于纠正失败原因。"
                "reactivate 操作推荐填写：追加需求描述，新管道首轮自动注入，Agent 可立即看到新要求。"
            ),
        },
        "drop_metrics": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "重试时要移除的评估指标 ID 列表（retry 操作可选）。"
                "移除后的指标不再参与评估，降低重试难度。"
                "适用于：某些指标过于严格、不适用于当前修复方案、"
                "或多次重试仍然失败需要降级验收标准的场景。"
                "示例：['semantic_check', 'performance_check']"
            ),
        },
        "include_details": {
            "type": "boolean",
            "default": False,
            "description": ("get 操作时是否包含详情"
                            "（description、result、metadata）。"
                            "默认 false 只返回摘要字段。"),
        },
        "include_agent_calls": {
            "type": "boolean",
            "default": False,
            "description": ("get 操作时是否包含 agent 调用记录"
                            "（metadata 中的 pipeline_history 等）。"
                            "需要 include_details=true 才生效。"),
        },
        "container_reason": {
            "type": "string",
            "description": "容器操作原因（complete_container/fail_container 操作时填写）",
        },
    },
    "required": ["action"],
}

TASK_MANAGE_DESCRIPTION = (
    "任务管理工具。支持获取、列表、暂停、恢复、取消、重试、重新激活、消息注入等操作。"
    "reactivate 用于重新激活已完成任务并追加新需求。"
    "还支持容器完成和容器失败操作（仅限主 Agent）。"
    "普通任务完成请使用 task_evaluate 工具。"
)


def task_manage_func(params: dict[str, Any]) -> dict[str, Any]:
    """执行任务管理操作。

    通过 ctx.get_service("task_service") 获取 TaskService，
    通过 ctx.get_service("message_queue") 获取 MessageQueue（inject 操作）。

    Args:
        params: 工具参数，含 action 和对应操作参数

    Returns:
        包含 success 和操作结果的字典
    """
    action = params.get("action")
    parent_agent_level = params.get("parent_agent_level")
    if not action:
        return {
            "success": False,
            "error": "必须提供 action 参数",
            "error_code": "MISSING_ACTION",
        }

    # BUG-FIX-P9：使用单例 TaskService，避免每次调用新建实例
    task_service = _get_task_service()
    if task_service is None:
        return {
            "success": False,
            "error": "TaskService 不可用",
            "error_code": "SERVICE_UNAVAILABLE",
        }

    # 分发操作
    dispatchers = {
        "get": _action_get,
        "list": _action_list,
        "status": _action_status,
        "pause": _action_pause,
        "resume": _action_resume,
        "cancel": _action_cancel,
        "retry": _action_retry,
        "reactivate": _action_reactivate,
        "inject": _action_inject,
        "complete_container": lambda svc, p: _action_container_op(svc, p, "complete"),
        "fail_container": lambda svc, p: _action_container_op(svc, p, "fail"),
    }

    handler = dispatchers.get(action)
    if handler is None:
        return {
            "success": False,
            "error": f"不支持的操作: {action}",
            "error_code": "INVALID_ACTION",
        }

    return handler(task_service, params)


def _action_get(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """获取单个任务详情。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id

    Returns:
        包含任务详情的字典
    """
    task_id = params.get("task_id")
    if not task_id:
        return {
            "success": False,
            "error": "get 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    task = task_service.get_task(task_id)
    if task is None:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }

    result = {
        "success": True,
        "task": _task_to_dict(
            task,
            include_details=params.get("include_details", False),
            include_agent_calls=params.get("include_agent_calls", False),
        ),
    }
    # 始终附带工作空间信息，方便 agent 在重试策略步骤⑤使用 inherit_workspace_from
    ws_meta = task.metadata.get("ws_meta") if task.metadata else None
    if ws_meta and isinstance(ws_meta, dict):
        result["workspace_path"] = ws_meta.get("path", "")
        result["project_root"] = ws_meta.get("project_root", "")
    return result


def _action_list(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """按状态列出任务。

    Args:
        task_service: TaskService 实例
        params: 工具参数，可选含 status_filter

    Returns:
        包含任务列表的字典
    """
    from tasks.types import TaskStatus

    status_filter = params.get("status_filter")
    if status_filter:
        try:
            status = TaskStatus(status_filter)
        except ValueError:
            return {
                "success": False,
                "error": f"无效的状态: {status_filter}",
                "error_code": "INVALID_STATUS",
            }
        tasks = task_service.list_by_status(status)
    else:
        # 列出所有状态的任务
        tasks = []
        for status in TaskStatus:
            tasks.extend(task_service.list_by_status(status))

    return {
        "success": True,
        "tasks": [_task_to_dict(t) for t in tasks],
        "count": len(tasks),
    }


def _action_status(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """获取任务状态。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id

    Returns:
        包含任务状态的字典
    """
    task_id = params.get("task_id")
    if not task_id:
        return {
            "success": False,
            "error": "status 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    task = task_service.get_task(task_id)
    if task is None:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }

    return {
        "success": True,
        "task_id": task.id,
        "status": task.status.value,
        "title": task.title,
    }


def _action_pause(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """暂停任务（running → paused）。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id

    Returns:
        操作结果字典
    """
    from tasks.state_machine import InvalidTransitionError

    task_id = params.get("task_id")
    if not task_id:
        return {
            "success": False,
            "error": "pause 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    try:
        task = task_service.pause_task(task_id)
        logger.info("[task_manage] 任务已暂停: %s", task_id)
        return {
            "success": True,
            "task_id": task.id,
            "status": task.status.value,
            "message": f"任务 {task_id} 已暂停",
        }
    except KeyError:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }
    except InvalidTransitionError as exc:
        return {
            "success": False,
            "error": f"状态转换不合法: {exc}",
            "error_code": "INVALID_TRANSITION",
        }


def _action_resume(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """恢复任务（paused → running）。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id

    Returns:
        操作结果字典
    """
    from tasks.state_machine import InvalidTransitionError

    task_id = params.get("task_id")
    if not task_id:
        return {
            "success": False,
            "error": "resume 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    try:
        task = task_service.resume_task(task_id)
        logger.info("[task_manage] 任务已恢复: %s", task_id)
        return {
            "success": True,
            "task_id": task.id,
            "status": task.status.value,
            "message": f"任务 {task_id} 已恢复",
        }
    except KeyError:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }
    except InvalidTransitionError as exc:
        return {
            "success": False,
            "error": f"状态转换不合法: {exc}",
            "error_code": "INVALID_TRANSITION",
        }


def _cancel_running_pipeline(task_id: str) -> None:
    """取消任务关联的运行中管道（best-effort）。

    BUG-FIX: cancel 操作只修改任务状态但没有停止 PipelineEngine，
    导致管道继续执行、LLM 调用浪费资源。通过 TaskWorker.cancel_pipeline
    强制取消 asyncio.Task，触发 PipelineEngine 的 CancelledError。
    """
    import sys

    from infrastructure.service_provider import get_service_provider
    task_worker = get_service_provider().get("task_worker")
    if task_worker is None:
        logger.debug("[task_manage] cancel: TaskWorker 不可用，跳过管道取消")
        return

    try:
        cancelled = task_worker.cancel_pipeline(task_id)
        if cancelled:
            logger.info(
                "[task_manage] cancel: 运行中管道已取消: task_id=%s",
                task_id,
            )
    except Exception as e:
        logger.warning(
            "[task_manage] cancel: 管道取消失败: task_id=%s, error=%s",
            task_id, e,
        )


def _action_cancel(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """取消任务（→ failed）。

    将任务标记为失败并记录取消原因。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id，可选含 reason

    Returns:
        操作结果字典
    """
    from tasks.state_machine import InvalidTransitionError

    task_id = params.get("task_id")
    if not task_id:
        return {
            "success": False,
            "error": "cancel 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    reason = params.get("reason", "用户取消")

    try:
        task = task_service.fail_task(task_id, error=reason)
        _cancel_running_pipeline(task_id)
        logger.info("[task_manage] 任务已取消: %s — %s", task_id, reason)
        return {
            "success": True,
            "task_id": task.id,
            "status": task.status.value,
            "message": f"任务 {task_id} 已取消",
        }
    except KeyError:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }
    except InvalidTransitionError as exc:
        return {
            "success": False,
            "error": f"状态转换不合法: {exc}",
            "error_code": "INVALID_TRANSITION",
        }


def _validate_and_get_task(
    task_service: Any,
    params: dict[str, Any],
    action_name: str,
) -> tuple[str, Any, None] | tuple[None, None, dict[str, Any]]:
    """验证 task_id 并获取任务，失败时返回 (None, None, error_dict)。"""
    task_id = params.get("task_id")
    if not task_id:
        return None, None, {
            "success": False,
            "error": f"{action_name} 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    task = task_service.get_task(task_id)
    if task is None:
        return None, None, {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }
    return task_id, task, None


def _apply_drop_metrics(task: Any, params: dict[str, Any]) -> list[str]:
    """从任务的评估指标中移除指定指标。

    Args:
        task: TaskModel 实例
        params: 工具参数，可含 drop_metrics 列表

    Returns:
        实际移除的指标 ID 列表
    """
    drop_metrics = params.get("drop_metrics")
    if not drop_metrics or not isinstance(drop_metrics, list):
        return []

    metadata = task.metadata if task.metadata else {}
    metric_ids = metadata.get("evaluation_metric_ids", [])
    ac = metadata.get("acceptance_criteria", {})
    retry_counts = metadata.get("eval_retry_count", {})

    dropped: list[str] = []
    for mid in drop_metrics:
        if mid in metric_ids:
            metric_ids.remove(mid)
            dropped.append(mid)
        if isinstance(ac, dict):
            ac.pop(mid, None)
        if isinstance(retry_counts, dict):
            retry_counts.pop(mid, None)

    metadata["evaluation_metric_ids"] = metric_ids
    metadata["acceptance_criteria"] = ac
    metadata["eval_retry_count"] = retry_counts
    task.metadata = metadata

    if dropped:
        logger.info(
            "[task_manage] 移除评估指标 | task_id=%s | dropped=%s",
            task.id, dropped,
        )
    return dropped


def _start_task_and_emit(
    task_service: Any,
    task: Any,
    task_id: str,
    fallback_running: bool = False,
) -> Any:
    """启动任务并发布 task.submitted 事件。

    BUG-FIX-P7：重试后发布 task.submitted 事件，触发 TaskWorker 重新执行。

    Args:
        task_service: TaskService 实例
        task: TaskModel 实例
        task_id: 任务 ID
        fallback_running: start_task 失败时是否直接设为 RUNNING

    Returns:
        更新后的 task（可能来自 start_task 或原 task）
    """
    updated_task = task
    try:
        updated_task = task_service.start_task(task_id)
    except Exception:
        if fallback_running:
            from tasks.types import TaskStatus
            from datetime import datetime
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now().isoformat()
            task_service._storage.save(task)
    _retry_emit_event(task_id)
    return updated_task


def _action_retry(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """重试失败任务（failed → running）。

    将 failed 状态的任务重新启动。当前状态机不支持
    failed → running 直接转换，需要先重置状态为 pending
    再启动。

    可选参数 message：重试时携带补充信息。消息会在新管道启动后
    由 MessageInjectPlugin 自动注入到会话中，Agent 第一轮即可看到。
    可选参数 drop_metrics：移除指定评估指标，降低重试难度。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id，可选含 message、drop_metrics

    Returns:
        操作结果字典
    """
    task_id, task, err = _validate_and_get_task(task_service, params, "retry")
    if err:
        return err

    from tasks.types import TaskStatus

    if task.status != TaskStatus.FAILED:
        return {
            "success": False,
            "error": f"只能重试失败的任务，当前状态: {task.status.value}",
            "error_code": "INVALID_STATUS",
        }

    dropped_names = _apply_drop_metrics(task, params)

    message_content = params.get("message")
    message_injected = False
    if message_content and task.pipeline_run_id:
        message_injected = _push_retry_message(
            task_id, task.pipeline_run_id, message_content, params,
        )

    task.status = TaskStatus.PENDING
    task.error = None
    task_service._storage.save(task)

    try:
        task = _start_task_and_emit(task_service, task, task_id)
        logger.info("[task_manage] 任务重试成功: %s", task_id)

        msg = f"任务 {task_id} 已重新启动"
        if dropped_names:
            msg += f"，已移除指标: {', '.join(dropped_names)}"
        if message_injected:
            msg += "，并已注入补充信息"
        result = {"success": True, "task_id": task_id, "status": task.status.value, "message": msg}
        # 附带工作空间信息，供 agent 在步骤⑤使用 inherit_workspace_from 时参考
        ws_meta = task.metadata.get("ws_meta") if task.metadata else None
        if ws_meta and isinstance(ws_meta, dict):
            result["workspace_path"] = ws_meta.get("path", "")
            result["project_root"] = ws_meta.get("project_root", "")
        return result
    except Exception as exc:
        return {"success": False, "error": f"重试失败: {exc}", "error_code": "RETRY_FAILED"}


def _action_reactivate(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """重新激活已完成任务（completed → running），携带追加需求。

    与 retry 的区别：
    - retry: 失败任务重新执行，从失败点继续
    - reactivate: 成功任务追加工作，在同一任务上下文中继续

    使用场景：任务已完成但需要追加修改、补充需求、修复遗漏等。
    原管道上下文（pipeline_history）保留在 metadata 中供追溯。
    也支持 drop_metrics 移除不适用的评估指标。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id，可选含 message、drop_metrics

    Returns:
        操作结果字典
    """
    from tasks.state_machine import InvalidTransitionError
    from tasks.types import TaskStatus

    task_id, task, err = _validate_and_get_task(task_service, params, "reactivate")
    if err:
        return err

    message_content = params.get("message")

    if task.status != TaskStatus.COMPLETED:
        return {
            "success": False,
            "error": f"只能重新激活已完成的任务，当前状态: {task.status.value}。"
                     f"失败任务请使用 retry，运行中任务请使用 inject。",
            "error_code": "INVALID_STATUS",
        }

    try:
        task = task_service.reactivate_task(task_id, message=message_content or "")
    except (KeyError, InvalidTransitionError) as exc:
        return {"success": False, "error": f"重新激活失败: {exc}", "error_code": "REACTIVATE_FAILED"}

    dropped_names = _apply_drop_metrics(task, params)

    if message_content:
        _push_retry_message(
            task_id, task.pipeline_run_id or "", message_content, params,
        )

    task = _start_task_and_emit(task_service, task, task_id, fallback_running=True)

    msg = f"任务 {task_id} 已重新激活，新管道即将启动"
    if message_content:
        msg += f"，追加需求: {message_content[:100]}"
    if dropped_names:
        msg += f"，已移除指标: {', '.join(dropped_names)}"
    return {"success": True, "task_id": task_id, "status": task.status.value, "message": msg}


def _push_retry_message(
    task_id: str, pipeline_id: str, content: str, params: dict[str, Any],
) -> bool:
    """为 retry 操作推送补充消息到 MessageQueue。

    Args:
        task_id: 任务 ID
        pipeline_id: 目标管道 ID（= task.pipeline_run_id）
        content: 消息内容
        params: 原始工具参数（用于获取 _message_queue）

    Returns:
        是否成功推送
    """
    import asyncio

    from infrastructure.message_queue import Message, create_message_id

    queue = params.get("_message_queue")
    if queue is None:
        logger.warning("[task_manage] retry: MessageQueue 不可用，跳过消息注入")
        return False

    message = Message(
        id=create_message_id(),
        pipeline_id=pipeline_id,
        target_id=task_id,
        content=content,
        priority=8,  # 高优先级，确保新管道第一轮就能消费
        metadata={"source": "task_manage_retry", "task_id": task_id},
    )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(queue.push(message))
        else:
            loop.run_until_complete(queue.push(message))
    except RuntimeError:
        try:
            asyncio.run(queue.push(message))
        except Exception as e:
            logger.error("[task_manage] retry: 消息推送失败: %s", e)
            return False
    except Exception as e:
        logger.error("[task_manage] retry: 消息推送失败: %s", e)
        return False

    logger.info(
        "[task_manage] retry: 补充消息已推送 | task_id=%s | pipeline=%s",
        task_id, pipeline_id,
    )
    return True


def _action_inject(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """向任务会话注入消息。

    通过 MessageQueue.push 将消息推入队列，由 MessageInjectPlugin
    在管道输入阶段自动消费。需要任务处于运行状态（有 pipeline_run_id）。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id、message

    Returns:
        操作结果字典
    """
    task_id = params.get("task_id")
    message_content = params.get("message")

    if not task_id:
        return {
            "success": False,
            "error": "inject 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }
    if not message_content:
        return {
            "success": False,
            "error": "inject 操作必须提供 message",
            "error_code": "MISSING_MESSAGE",
        }

    # 检查任务是否存在
    task = task_service.get_task(task_id)
    if task is None:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }

    # 从任务的 pipeline_run_id 获取管道路由地址
    pipeline_id = task.pipeline_run_id
    if not pipeline_id:
        return {
            "success": False,
            "error": f"任务 {task_id} 没有关联的管道实例，无法注入消息。"
                     "请确认任务是否正在运行。",
            "error_code": "NO_PIPELINE",
        }

    # 获取 MessageQueue
    queue = params.get("_message_queue")
    if queue is None:
        return {
            "success": False,
            "error": "MessageQueue 服务未注入，无法执行 inject 操作。",
            "error_code": "SERVICE_UNAVAILABLE",
        }

    # 构造并推入消息
    import asyncio

    from infrastructure.message_queue import Message, create_message_id

    message = Message(
        id=create_message_id(),
        pipeline_id=pipeline_id,
        target_id=task_id,
        content=message_content,
        priority=params.get("priority", 5),
        metadata={"source": "task_manage", "task_id": task_id},
    )

    # BUG-FIX-P8：_action_inject 是同步函数，不能直接 await，
    # 需要判断事件循环状态来安全调用异步 push
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(queue.push(message))
        else:
            loop.run_until_complete(queue.push(message))
    except RuntimeError:
        try:
            asyncio.run(queue.push(message))
        except Exception as e:
            logger.error("[task_manage] inject: 消息推送失败: %s", e)
            return {
                "success": False,
                "error": f"消息推送失败: {e}",
                "error_code": "INJECT_FAILED",
            }

    logger.info(
        "[task_manage] 消息已注入 | task_id=%s | pipeline=%s | content_len=%d",
        task_id, pipeline_id, len(message_content),
    )

    return {
        "success": True,
        "task_id": task_id,
        "message_id": message.id,
        "message": f"消息已注入到任务 {task_id}",
    }


def _action_container_op(task_service: Any, params: dict[str, Any], op: str) -> dict[str, Any]:
    """容器操作：完成或失败（仅限 L1 主 Agent）。

    将容器任务（父任务）标记为 completed 或 failed。
    前提条件：
    1. 调用者必须是 L1 主 Agent（parent_agent_level == 1）
    2. 目标任务必须是容器（有子任务）
    3. 目标任务当前状态必须是 PENDING

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id、可选含 container_reason
        op: 操作类型，"complete" 或 "fail"

    Returns:
        操作结果字典
    """
    from tasks.state_machine import InvalidTransitionError
    from tasks.types import TaskStatus

    parent_agent_level = params.get("parent_agent_level", 1)

    # 权限检查：仅 L1 主 Agent 可操作
    if parent_agent_level != 1:
        return {
            "success": False,
            "error": "容器操作仅限 L1 主 Agent 执行",
            "error_code": "PERMISSION_DENIED",
        }

    task_id = params.get("task_id")
    if not task_id:
        return {
            "success": False,
            "error": f"{op}_container 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    task = task_service.get_task(task_id)
    if task is None:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }

    # 验证是容器任务（有子任务）
    subtasks = task_service.list_subtasks(task_id)
    if not subtasks:
        return {
            "success": False,
            "error": f"任务 {task_id} 不是容器任务（无子任务），不能使用容器操作",
            "error_code": "NOT_A_CONTAINER",
        }

    # 验证当前状态为 PENDING（容器在子任务执行期间保持 PENDING）
    if task.status != TaskStatus.PENDING:
        return {
            "success": False,
            "error": f"容器当前状态为 {task.status.value}，只能操作 PENDING 状态的容器",
            "error_code": "INVALID_STATUS",
        }

    reason = params.get("container_reason", params.get("reason", ""))

    try:
        if op == "complete":
            task_service._transition_with_callback(task, TaskStatus.COMPLETED)
            from datetime import datetime
            task.completed_at = datetime.now().isoformat()
            task_service._storage.save(task)
            logger.info("[task_manage] 容器已完成: %s — %s", task_id, reason)
            return {
                "success": True,
                "task_id": task.id,
                "status": "completed",
                "message": f"容器 {task_id} 已标记为完成",
                "subtask_count": len(subtasks),
                "completed_subtasks": sum(1 for s in subtasks if s.status == TaskStatus.COMPLETED),
            }
        else:  # fail
            task_service._transition_with_callback(task, TaskStatus.FAILED)
            if reason:
                task.error = reason
                task_service._storage.save(task)
            logger.info("[task_manage] 容器已标记失败: %s — %s", task_id, reason)
            return {
                "success": True,
                "task_id": task.id,
                "status": "failed",
                "message": f"容器 {task_id} 已标记为失败",
                "reason": reason,
            }
    except InvalidTransitionError as exc:
        return {
            "success": False,
            "error": f"容器状态转换不合法: {exc}",
            "error_code": "INVALID_TRANSITION",
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"容器操作失败: {exc}",
            "error_code": "OPERATION_FAILED",
        }

_MAX_HISTORY_ITEMS = 5
_MAX_TOOL_RESULT_LEN = 200


def _summarize_history(history: Any) -> Any:
    """将 pipeline_history / agent_calls 压缩为摘要。

    只保留最近几轮，截断大字段，避免返回值膨胀。

    Args:
        history: 原始历史记录（list / dict / 其他）

    Returns:
        摘要后的数据
    """
    if not history:
        return history
    if isinstance(history, list):
        total = len(history)
        items = history[-_MAX_HISTORY_ITEMS:]
        return {
            "total": total,
            "showing": len(items),
            "items": [_trim_item(i) for i in items],
        }
    if isinstance(history, dict):
        return {
            "total_entries": len(history),
            "keys": list(history.keys())[:_MAX_HISTORY_ITEMS],
        }
    return history


def _trim_item(item: Any) -> Any:
    """截断单条记录中的大文本字段。"""
    if not isinstance(item, dict):
        return item
    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, str) and len(v) > _MAX_TOOL_RESULT_LEN:
            out[k] = v[:_MAX_TOOL_RESULT_LEN] + "...<truncated>"
        elif isinstance(v, list) and len(v) > 3:
            out[k] = v[:3]
            out[k].append(f"...<+{len(v) - 3} more>")
        else:
            out[k] = v
    return out


def _task_to_dict(
    task: Any,
    include_details: bool = False,
    include_agent_calls: bool = False,
) -> dict[str, Any]:
    """将 TaskModel 转换为可序列化字典。

    默认只返回摘要字段（id、title、status 等核心信息），
    避免将 metadata 中的 pipeline_history 等大体量字段
    无条件序列化。

    Args:
        task: TaskModel 实例
        include_details: 是否包含 description、result、
            metadata（不含 pipeline_history）
        include_agent_calls: 是否在 metadata 中保留
            pipeline_history 等调用记录。
            需要 include_details=True 才生效。

    Returns:
        可 JSON 序列化的字典
    """
    d: dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "agent_level": task.agent_level.value,
        "agent_name": task.agent_name,
        "parent_task_id": task.parent_task_id,
        "pipeline_run_id": task.pipeline_run_id,
        "error": task.error,
        "reject_count": task.reject_count,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
    }

    if include_details:
        d["description"] = task.description
        d["result"] = task.result
        d["target_type"] = task.target_type
        d["execution_record_id"] = task.execution_record_id
        d["dependencies"] = task.dependencies

        meta = dict(task.metadata) if task.metadata else {}
        if include_agent_calls:
            meta["pipeline_history"] = _summarize_history(
                meta.get("pipeline_history"),
            )
            meta["agent_calls"] = _summarize_history(
                meta.get("agent_calls"),
            )
        else:
            for key in ("pipeline_history", "agent_calls"):
                meta.pop(key, None)
        d["metadata"] = meta

    return d
