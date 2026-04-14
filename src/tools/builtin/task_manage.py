"""任务管理工具 — 对任务进行生命周期操作。

操作列表：
- get: 获取单个任务详情
- list: 按状态列出任务
- status: 获取任务状态
- pause: 暂停任务（running → paused）
- resume: 恢复任务（paused → running）
- cancel: 取消任务（→ failed）
- retry: 重试失败任务（failed → running）
- inject: 向任务会话注入消息

通过 ctx.get_service("task_service") 获取 TaskService，
通过 ctx.get_service("message_queue") 获取 MessageQueue（inject 操作需要）。

简化原则：
- 去掉权限检查（L1/L2 分级）
- 去掉 delete 操作
- 禁止设为 completed（完成只能通过 task_evaluate）

暴露接口：
- task_manage_schema: 工具参数 JSON Schema
- task_manage_func: 工具执行函数
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 工具参数 Schema（OpenAI Function Calling 格式）
task_manage_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get", "list", "status", "pause", "resume", "cancel", "retry", "inject"],
            "description": "操作类型",
        },
        "task_id": {
            "type": "string",
            "description": "任务 ID（get/status/pause/resume/cancel/retry/inject 操作必填）",
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
            "description": "注入消息内容（inject 操作必填）",
        },
        "session_id": {
            "type": "string",
            "description": "注入消息的目标会话 ID（inject 操作必填）",
        },
    },
    "required": ["action"],
}

TASK_MANAGE_DESCRIPTION = (
    "任务管理工具。支持获取、列表、暂停、恢复、取消、重试和消息注入等操作。"
    "完成操作请使用 task_evaluate 工具。"
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
    if not action:
        return {
            "success": False,
            "error": "必须提供 action 参数",
            "error_code": "MISSING_ACTION",
        }

    # 获取 TaskService
    try:
        from tasks.service import TaskService

        task_service = TaskService()
    except Exception as exc:
        return {
            "success": False,
            "error": f"TaskService 不可用: {exc}",
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
        "inject": _action_inject,
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

    return {
        "success": True,
        "task": _task_to_dict(task),
    }


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


def _action_retry(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """重试失败任务（failed → running）。

    将 failed 状态的任务重新启动。当前状态机不支持
    failed → running 直接转换，需要先重置状态为 pending
    再启动。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id

    Returns:
        操作结果字典
    """
    task_id = params.get("task_id")
    if not task_id:
        return {
            "success": False,
            "error": "retry 操作必须提供 task_id",
            "error_code": "MISSING_TASK_ID",
        }

    task = task_service.get_task(task_id)
    if task is None:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }

    # 重置为 pending 再启动
    # 当前状态机不支持 failed → pending 直接转换，
    # 直接修改 task 状态再通过 storage 保存
    from tasks.types import TaskStatus

    if task.status != TaskStatus.FAILED:
        return {
            "success": False,
            "error": f"只能重试失败的任务，当前状态: {task.status.value}",
            "error_code": "INVALID_STATUS",
        }

    # 通过 TaskStorage 直接更新（绕过状态机终态限制）
    task.status = TaskStatus.PENDING
    task.error = None
    task_service._storage.save(task)

    try:
        task = task_service.start_task(task_id)
        logger.info("[task_manage] 任务重试成功: %s", task_id)
        return {
            "success": True,
            "task_id": task.id,
            "status": task.status.value,
            "message": f"任务 {task_id} 已重新启动",
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"重试失败: {exc}",
            "error_code": "RETRY_FAILED",
        }


def _action_inject(task_service: Any, params: dict[str, Any]) -> dict[str, Any]:
    """向任务会话注入消息。

    通过 MessageQueue.push 将消息推入队列，由 MessageInjectPlugin
    在管道输入阶段自动消费。

    Args:
        task_service: TaskService 实例
        params: 工具参数，需含 task_id、message、session_id

    Returns:
        操作结果字典
    """
    task_id = params.get("task_id")
    message_content = params.get("message")
    session_id = params.get("session_id")

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
    if not session_id:
        return {
            "success": False,
            "error": "inject 操作必须提供 session_id",
            "error_code": "MISSING_SESSION_ID",
        }

    # 检查任务是否存在
    task = task_service.get_task(task_id)
    if task is None:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
            "error_code": "TASK_NOT_FOUND",
        }

    # 获取 MessageQueue
    # 工具函数无法直接访问 PluginContext，
    # 通过 params["_message_queue"] 注入（由 ToolCore 在执行时传入）
    queue = params.get("_message_queue")
    if queue is None:
        return {
            "success": False,
            "error": "MessageQueue 服务未注入，无法执行 inject 操作。"
                     "请在管道配置中确保 message_queue 服务已注册。",
            "error_code": "SERVICE_UNAVAILABLE",
        }

    # 构造并推入消息
    from infrastructure.message_queue import Message, create_message_id

    message = Message(
        id=create_message_id(),
        session_id=session_id,
        target_id=task_id,
        content=message_content,
        priority=params.get("priority", 5),
        metadata={"source": "task_manage", "task_id": task_id},
    )

    queue.push(message)

    logger.info(
        "[task_manage] 消息已注入 | task_id=%s | session_id=%s | content_len=%d",
        task_id, session_id, len(message_content),
    )

    return {
        "success": True,
        "task_id": task_id,
        "message_id": message.id,
        "message": f"消息已注入到会话 {session_id}",
    }


def _task_to_dict(task: Any) -> dict[str, Any]:
    """将 TaskModel 转换为可序列化字典。

    Args:
        task: TaskModel 实例

    Returns:
        可 JSON 序列化的字典
    """
    from dataclasses import asdict

    d = asdict(task)
    d["status"] = task.status.value
    d["priority"] = task.priority.value
    d["agent_level"] = task.agent_level.value
    return d
