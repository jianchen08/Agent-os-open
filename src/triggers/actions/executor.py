"""
动作执行器

执行触发器的各种动作：通知、API 调用、任务操作等。
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from jinja2 import Template

from src.config.settings import get_settings
from src.core.di import get_global_container
from src.triggers.models import ActionConfig, ActionType, ExecutionResult

settings = get_settings()

logger = logging.getLogger(__name__)


class ActionExecutor:
    """
    动作执行器

    负责执行各种类型的触发器动作。
    """

    def __init__(self):
        self._action_handlers = {
            ActionType.NOTIFICATION: self._execute_notification,
            ActionType.API_CALL: self._execute_api_call,
            ActionType.TASK_RETRY: self._execute_task_retry,
            ActionType.TASK_COMPLETE: self._execute_task_complete,
            ActionType.CUSTOM: self._execute_custom,
        }
        self._custom_handlers = {}
        self._retry_queue = []

    async def execute(self, action_config: ActionConfig, context: dict[str, Any]) -> ExecutionResult:
        handler = self._action_handlers.get(action_config.type)

        if not handler:
            return ExecutionResult(
                success=False,
                message=f"未知的动作类型: {action_config.type}",
                error=f"No handler for {action_config.type}",
            )

        try:
            return await handler(action_config.config, context)
        except Exception as e:
            logger.error(f"执行动作失败: {e}", exc_info=True)
            return ExecutionResult(success=False, message=f"动作执行失败: {str(e)}", error=str(e))

    async def _execute_notification(self, config: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
        channel = config.get("channel", "websocket")
        template_str = config.get("template", "")
        priority = config.get("priority", "normal")
        title = config.get("title", "系统通知")
        user_id = config.get("user_id") or context.get("user_id")

        try:
            template = Template(template_str)
            message = template.render(**context)
        except Exception as e:
            logger.error(f"渲染消息模板失败: {e}")
            message = template_str

        try:
            if channel == "websocket":
                await self._send_websocket_notification(user_id, title, message, priority)
            elif channel == "database":
                await self._send_database_notification(user_id, title, message, priority)
            elif channel == "webhook":
                await self._send_webhook_notification(config, title, message, priority)
            else:
                logger.warning(f"不支持的通知渠道: {channel}")
                return ExecutionResult(
                    success=False,
                    message=f"不支持的通知渠道: {channel}",
                    error=f"Unsupported channel: {channel}",
                )

            logger.info(f"发送通知 [{channel}] {priority}: {title}")

            return ExecutionResult(
                success=True,
                message=f"通知已发送到 {channel}",
                data={
                    "channel": channel,
                    "title": title,
                    "message": message,
                    "priority": priority,
                    "user_id": user_id,
                },
            )

        except Exception as e:
            logger.error(f"发送通知失败: {e}")
            return ExecutionResult(success=False, message=f"发送通知失败: {str(e)}", error=str(e))

    async def _send_websocket_notification(self, user_id: str, title: str, message: str, priority: str):
        try:
            container = get_global_container()

            if container.has("websocket_event_service"):
                event_service = container.get("websocket_event_service")
                await event_service.send_trigger_notification(
                    user_id=user_id,
                    title=title,
                    message=message,
                    priority=priority,
                    timestamp=datetime.utcnow(),
                )
                logger.info(f"WebSocket通知已发送: 用户={user_id}, 标题={title}, 优先级={priority}")
            else:
                logger.warning("WebSocket事件服务未注册，使用日志记录")
                logger.info(f"WebSocket通知: 用户={user_id}, 标题={title}, 优先级={priority}")

        except Exception as e:
            logger.error(f"发送WebSocket通知失败: {e}")
            logger.info(f"WebSocket通知(降级): 用户={user_id}, 标题={title}, 优先级={priority}")

    async def _send_database_notification(self, user_id: str, title: str, message: str, priority: str):
        try:
            from src.api.services.notification_service import NotificationService  # noqa: PLC0415

            if user_id:
                service = NotificationService()
                await service.create_notification(
                    user_id=UUID(user_id),
                    title=title,
                    message=message,
                    notification_type="info",
                    priority=priority,
                )
                logger.info(f"数据库通知已创建: 用户={user_id}, 标题={title}")
            else:
                logger.warning("缺少用户ID，无法创建数据库通知")

        except Exception as e:
            logger.error(f"创建数据库通知失败: {e}")
            raise

    async def _send_webhook_notification(self, config: dict[str, Any], title: str, message: str, priority: str):
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Webhook通知需要提供webhook_url")

        payload = {
            "title": title,
            "message": message,
            "priority": priority,
            "timestamp": datetime.utcnow().isoformat(),
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(webhook_url, json=payload)
                response.raise_for_status()
                logger.info(f"Webhook通知已发送: {webhook_url}")
        except Exception as e:
            logger.error(f"发送Webhook通知失败: {e}")
            raise

    async def _execute_api_call(self, config: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
        endpoint = config.get("endpoint", "")
        method = config.get("method", "GET").upper()
        headers = config.get("headers", {})
        body = config.get("body", {})
        timeout = config.get("timeout", 30)

        base_url = context.get("base_url", settings.api_base_url)
        url = f"{base_url}{endpoint}"

        if isinstance(body, dict):
            try:
                template = Template(str(body))
                rendered = template.render(**context)
                import json  # noqa: PLC0415

                body = json.loads(rendered)
            except Exception as e:
                logger.debug(f"请求体渲染失败或不需要: {e}")

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    response = await client.get(url, headers=headers, params=body)
                elif method == "POST":
                    response = await client.post(url, headers=headers, json=body)
                elif method == "PUT":
                    response = await client.put(url, headers=headers, json=body)
                elif method == "DELETE":
                    response = await client.delete(url, headers=headers)
                else:
                    return ExecutionResult(
                        success=False,
                        message=f"不支持的 HTTP 方法: {method}",
                        error=f"Unsupported method: {method}",
                    )

                response.raise_for_status()

                return ExecutionResult(
                    success=True,
                    message=f"API 调用成功: {method} {endpoint}",
                    data={
                        "status_code": response.status_code,
                        "response": (
                            response.json()
                            if response.headers.get("content-type", "").startswith("application/json")
                            else response.text
                        ),
                    },
                )

        except httpx.HTTPError as e:
            logger.error(f"API 调用失败: {e}")
            return ExecutionResult(success=False, message=f"API 调用失败: {str(e)}", error=str(e))

    async def _execute_task_retry(self, config: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
        task_id = context.get("task_id")
        max_retries = config.get("max_retries", 3)
        delay_seconds = config.get("delay_seconds", 60)
        exponential_backoff = config.get("exponential_backoff", False)

        if not task_id:
            return ExecutionResult(
                success=False,
                message="缺少任务ID，无法执行重试",
                error="Missing task_id in context",
            )

        current_retries = context.get("retry_count", 0)

        if current_retries >= max_retries:
            logger.warning(f"任务 {task_id} 已达到最大重试次数 {max_retries}")
            return ExecutionResult(
                success=False,
                message=f"任务 {task_id} 已达到最大重试次数",
                error="Max retries exceeded",
            )

        actual_delay = delay_seconds * 2**current_retries if exponential_backoff else delay_seconds

        retry_info = {
            "task_id": task_id,
            "retry_count": current_retries + 1,
            "scheduled_time": datetime.utcnow().timestamp() + actual_delay,
            "config": config,
            "context": context,
        }

        self._retry_queue.append(retry_info)

        logger.info(f"任务重试已安排: {task_id}, 重试次数: {current_retries + 1}/{max_retries}, 延迟: {actual_delay}s")

        return ExecutionResult(
            success=True,
            message=f"任务 {task_id} 已安排重试",
            data={
                "task_id": task_id,
                "retry_count": current_retries + 1,
                "max_retries": max_retries,
                "delay_seconds": actual_delay,
                "scheduled_time": retry_info["scheduled_time"],
            },
        )

    async def _execute_task_complete(self, config: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
        task_id = config.get("task_id") or context.get("task_id")
        status = config.get("status", "completed")
        result = config.get("result", {})

        if not task_id:
            return ExecutionResult(
                success=False,
                message="缺少任务ID，无法标记完成",
                error="Missing task_id",
            )

        try:
            container = get_global_container()

            if container.has("task_manager"):
                task_manager = container.get("task_manager")
                update_result = await task_manager.update_task_status(task_id=task_id, status=status, result=result)

                if update_result.get("success", False):
                    logger.info(f"任务完成: {task_id}, 状态: {status}")
                    return ExecutionResult(
                        success=True,
                        message=f"任务 {task_id} 已标记为 {status}",
                        data={"task_id": task_id, "status": status, "result": result},
                    )
                error_msg = update_result.get("error", "未知错误")
                return ExecutionResult(
                    success=False,
                    message=f"更新任务状态失败: {error_msg}",
                    error=error_msg,
                )
            logger.warning("任务管理器服务未注册，仅记录日志")
            logger.info(f"任务完成(降级): {task_id}, 状态: {status}")
            return ExecutionResult(
                success=True,
                message=f"任务 {task_id} 已标记为 {status} (降级模式)",
                data={"task_id": task_id, "status": status, "result": result},
            )

        except Exception as e:
            logger.error(f"执行任务完成动作失败: {e}")
            return ExecutionResult(success=False, message=f"执行任务完成动作失败: {str(e)}", error=str(e))

    async def _execute_custom(self, config: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
        handler_name = config.get("handler", "")
        params = config.get("params", {})

        if not handler_name:
            return ExecutionResult(
                success=False,
                message="缺少自定义处理器名称",
                error="Missing handler name",
            )

        handler = self._custom_handlers.get(handler_name)
        if not handler:
            return ExecutionResult(
                success=False,
                message=f"未找到自定义处理器: {handler_name}",
                error=f"Handler not found: {handler_name}",
            )

        try:
            result = await handler(params, context)

            logger.info(f"自定义动作执行成功: {handler_name}")

            return ExecutionResult(
                success=True,
                message=f"自定义动作 {handler_name} 已执行",
                data={"handler": handler_name, "params": params, "result": result},
            )

        except Exception as e:
            logger.error(f"自定义动作执行失败: {handler_name}, 错误: {e}")
            return ExecutionResult(
                success=False,
                message=f"自定义动作 {handler_name} 执行失败: {str(e)}",
                error=str(e),
            )

    def register_custom_handler(self, name: str, handler):
        self._custom_handlers[name] = handler
        logger.info(f"注册自定义处理器: {name}")

    def unregister_custom_handler(self, name: str) -> bool:
        if name in self._custom_handlers:
            del self._custom_handlers[name]
            logger.info(f"注销自定义处理器: {name}")
            return True
        return False

    def list_custom_handlers(self) -> list[str]:
        return list(self._custom_handlers.keys())

    async def process_retry_queue(self):
        """处理重试队列中已到期的任务"""
        current_time = datetime.utcnow().timestamp()
        ready_retries = []

        # 收集已到期的重试任务
        for i, retry_info in enumerate(self._retry_queue):
            if retry_info["scheduled_time"] <= current_time:
                ready_retries.append((i, retry_info))

        # 从队列中移除（倒序移除避免索引问题）
        for i, _ in reversed(ready_retries):
            self._retry_queue.pop(i)

        # 执行重试任务
        for _, retry_info in ready_retries:
            await self._execute_retry(retry_info)

    async def _execute_retry(self, retry_info: dict[str, Any]) -> None:
        """
        执行单个重试任务

        Args:
            retry_info: 重试任务信息
        """
        try:
            container = get_global_container()

            if not container.has("task_manager"):
                logger.error("无法获取任务管理器服务")
                return

            task_manager = container.get("task_manager")
            retry_result = await task_manager.retry_task(
                task_id=retry_info["task_id"],
                retry_count=retry_info["retry_count"],
                context=retry_info["context"],
            )

            if retry_result.get("success", False):
                logger.info(f"任务重试成功: {retry_info['task_id']}, 重试次数: {retry_info['retry_count']}")
            else:
                logger.error(f"任务重试失败: {retry_info['task_id']}, 错误: {retry_result.get('error', '未知错误')}")

        except Exception as e:
            logger.error(f"任务重试异常: {retry_info['task_id']}, 错误: {e}")

    def get_retry_queue_status(self) -> dict[str, Any]:
        current_time = datetime.utcnow().timestamp()
        pending_count = len(self._retry_queue)
        ready_count = sum(1 for retry in self._retry_queue if retry["scheduled_time"] <= current_time)

        return {
            "total_pending": pending_count,
            "ready_for_retry": ready_count,
            "waiting": pending_count - ready_count,
            "queue": [
                {
                    "task_id": retry["task_id"],
                    "retry_count": retry["retry_count"],
                    "scheduled_time": retry["scheduled_time"],
                    "ready": retry["scheduled_time"] <= current_time,
                }
                for retry in self._retry_queue
            ],
        }
