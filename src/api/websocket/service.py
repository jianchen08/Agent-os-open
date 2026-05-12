"""
WebSocket 事件推送服务

提供统一的 WebSocket 事件推送接口

事件分类：
1. 任务生命周期：project_*/task_*
2. 统一执行卡片：execution_start/progress/done/cancelled
3. 思考模式：thinking_start/chunk/end
4. 用户交互：clarification_needed/interaction_requested
5. 系统警告：cost_warning/resource_limit
"""

import logging
from typing import Any

from src.api.websocket import (
    ConnectionManager,
    create_auto_execute_triggered_event,
    create_clarification_needed_event,
    create_cost_warning_event,
    create_execution_cancelled_event,
    create_execution_done_event,
    create_execution_progress_event,
    create_execution_start_event,
    create_interaction_requested_event,
    create_resource_limit_event,
    create_task_ac_evaluated_event,
    create_task_completed_event,
    create_task_created_event,
    create_task_failed_event,
    create_task_phase_changed_event,
    create_thinking_chunk_event,
    create_thinking_end_event,
    create_thinking_start_event,
)

logger = logging.getLogger(__name__)


class WebSocketEventService:
    """
    WebSocket 事件推送服务

    核心职责：
    1. 提供统一的事件推送接口
    2. 处理推送失败的错误和重试
    3. 记录推送日志

    注意：Project 相关事件已废弃，系统已迁移到独立 Task 架构
    """

    def __init__(self, connection_manager: ConnectionManager):
        """
        初始化事件推送服务

        Args:
            connection_manager: 连接管理器
        """
        self.connection_manager = connection_manager

    # ========================================================================
    # 短期任务相关事件推送
    # ========================================================================

    async def send_task_created(
        self,
        user_id: str,
        taskId: str,
        goal: str,
        taskType: str,
        phase: str,
        projectId: str | None = None,
    ) -> bool:
        """
        推送短期任务创建事件

        Args:
            user_id: 用户 ID
            taskId: 任务 ID
            goal: 任务目标
            taskType: 任务类型
            phase: 当前阶段
            projectId: 所属项目 ID

        Returns:
            是否推送成功
        """
        try:
            event = create_task_created_event(
                taskId=taskId,
                goal=goal,
                taskType=taskType,
                phase=phase,
                projectId=projectId,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                f"推送 task_created 事件 | taskId={taskId} | type={taskType} | count={count}"
            )
            return count > 0
        except Exception as e:
            logger.error(f"推送 task_created 事件失败 | taskId={taskId} | error={e}")
            return False

    async def send_task_phase_changed(
        self,
        user_id: str,
        taskId: str,
        phase: str,
        status: str,
        timestamp,
    ) -> bool:
        """
        推送任务阶段变更事件

        Args:
            user_id: 用户 ID
            taskId: 任务 ID
            phase: 新阶段
            status: 阶段状态
            timestamp: 变更时间

        Returns:
            是否推送成功
        """
        try:
            event = create_task_phase_changed_event(
                taskId=taskId, phase=phase, status=status, timestamp=timestamp
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                f"推送 task_phase_changed 事件 | taskId={taskId} | phase={phase} | status={status} | count={count}"
            )
            return count > 0
        except Exception as e:
            logger.error(
                f"推送 task_phase_changed 事件失败 | taskId={taskId} | error={e}"
            )
            return False

    async def send_task_ac_evaluated(
        self,
        user_id: str,
        taskId: str,
        acId: str,
        passed: bool,
        result: dict[str, Any],
    ) -> bool:
        """
        推送 AC 评估完成事件

        Args:
            user_id: 用户 ID
            taskId: 任务 ID
            acId: 验收标准 ID
            passed: 是否通过
            result: 评估结果

        Returns:
            是否推送成功
        """
        try:
            event = create_task_ac_evaluated_event(
                taskId=taskId, acId=acId, passed=passed, result=result
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                f"推送 task_ac_evaluated 事件 | taskId={taskId} | acId={acId} | passed={passed} | count={count}"
            )
            return count > 0
        except Exception as e:
            logger.error(
                f"推送 task_ac_evaluated 事件失败 | taskId={taskId} | acId={acId} | error={e}"
            )
            return False

    async def send_task_completed(
        self,
        user_id: str,
        taskId: str,
        result: dict[str, Any],
        summary: str,
    ) -> bool:
        """
        推送任务完成事件

        Args:
            user_id: 用户 ID
            taskId: 任务 ID
            result: 执行结果
            summary: 执行总结

        Returns:
            是否推送成功
        """
        try:
            event = create_task_completed_event(
                taskId=taskId, result=result, summary=summary
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                f"推送 task_completed 事件 | taskId={taskId} | summary={summary[:50]}... | count={count}"
            )
            return count > 0
        except Exception as e:
            logger.error(f"推送 task_completed 事件失败 | taskId={taskId} | error={e}")
            return False

    async def send_task_failed(
        self,
        user_id: str,
        taskId: str,
        error: str,
        retryCount: int,
    ) -> bool:
        """
        推送任务失败事件

        Args:
            user_id: 用户 ID
            taskId: 任务 ID
            error: 错误信息
            retryCount: 重试次数

        Returns:
            是否推送成功
        """
        try:
            event = create_task_failed_event(
                taskId=taskId, error=error, retryCount=retryCount
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                f"推送 task_failed 事件 | taskId={taskId} | error={error[:50]}... | retry={retryCount} | count={count}"
            )
            return count > 0
        except Exception as e:
            logger.error(f"推送 task_failed 事件失败 | taskId={taskId} | error={e}")
            return False

    # ========================================================================
    # 自动执行事件推送 [已废弃]
    # ========================================================================

    async def send_auto_execute_triggered(
        self,
        user_id: str,
        projectId: str,
        taskId: str,
        timestamp,
    ) -> bool:
        """
        推送自动执行触发事件 [已废弃]

        Args:
            user_id: 用户 ID
            projectId: 项目 ID
            taskId: 任务 ID
            timestamp: 触发时间

        Returns:
            是否推送成功
        """
        import warnings

        warnings.warn(
            "send_auto_execute_triggered 已废弃，系统已迁移到独立 Task 架构",
            DeprecationWarning,
            stacklevel=2,
        )

        try:
            event = create_auto_execute_triggered_event(
                projectId=projectId, taskId=taskId, timestamp=timestamp
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                f"推送 auto_execute_triggered 事件 | projectId={projectId} | taskId={taskId} | count={count}"
            )
            return count > 0
        except Exception as e:
            logger.error(
                f"推送 auto_execute_triggered 事件失败 | projectId={projectId} | taskId={taskId} | error={e}"
            )
            return False

    # ========================================================================
    # 用户交互事件推送
    # ========================================================================

    async def send_clarification_needed(
        self,
        user_id: str,
        taskId: str,
        sessionId: str,
        tabId: str,
        questions: list,
        context: str | None = None,
    ) -> bool:
        """
        推送澄清请求事件

        Args:
            user_id: 用户 ID
            taskId: 任务 ID
            sessionId: 会话 ID
            tabId: Agent Tab ID
            questions: 需要澄清的问题列表
            context: 澄清上下文说明

        Returns:
            是否推送成功
        """
        try:
            event = create_clarification_needed_event(
                taskId=taskId,
                sessionId=sessionId,
                tabId=tabId,
                questions=questions,
                context=context,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                "推送 clarification_needed 事件 | "
                "taskId=%s | tabId=%s | questions=%d | count=%d",
                taskId,
                tabId,
                len(questions),
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 clarification_needed 事件失败 | taskId=%s | error=%s",
                taskId,
                e,
            )
            return False

    async def send_interaction_requested(
        self,
        user_id: str,
        request_id: str,
        interaction_type: str,
        source: str,
        source_id: str,
        title: str,
        description: str,
        context: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        timeout: int | None = None,
        priority: str = "normal",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """
        推送交互请求事件（统一审批/对话场景）

        Args:
            user_id: 用户 ID
            request_id: 交互请求 ID
            interaction_type: 交互类型 (approval/conversation)
            source: 来源 (agent/tool/workflow)
            source_id: 来源 ID
            title: 交互标题
            description: 交互描述
            context: 上下文信息
            options: 可选操作
            timeout: 超时时间（秒）
            priority: 优先级 (low/normal/high/urgent)
            metadata: 元数据

        Returns:
            是否推送成功
        """
        try:
            event = create_interaction_requested_event(
                requestId=request_id,
                interactionType=interaction_type,
                source=source,
                sourceId=source_id,
                title=title,
                description=description,
                context=context,
                options=options,
                timeout=timeout,
                priority=priority,
                metadata=metadata,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                "推送 interaction_requested 事件 | "
                "request_id=%s | type=%s | source=%s | count=%d",
                request_id,
                interaction_type,
                source,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 interaction_requested 事件失败 | request_id=%s | error=%s",
                request_id,
                e,
            )
            return False

    # ========================================================================
    # 统一执行卡片事件推送（工具/Agent/工作流）
    # ========================================================================

    async def send_execution_start(
        self,
        user_id: str,
        execution_id: str,
        execution_type: str,
        name: str,
        description: str | None = None,
        parent_id: str | None = None,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """
        推送执行开始事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            execution_type: 执行类型 (tool/agent/workflow)
            name: 名称
            description: 描述
            parent_id: 父执行 ID（嵌套时使用）
            input_data: 输入参数
            metadata: 元数据

        Returns:
            是否推送成功
        """
        try:
            event = create_execution_start_event(
                executionId=execution_id,
                executionType=execution_type,
                name=name,
                description=description,
                parentId=parent_id,
                input=input_data,
                metadata=metadata,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                "推送 execution_start 事件 | "
                "execution_id=%s | type=%s | name=%s | count=%d",
                execution_id,
                execution_type,
                name,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 execution_start 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    async def send_execution_progress(
        self,
        user_id: str,
        execution_id: str,
        progress: int,
        current_step: str | None = None,
        message: str | None = None,
    ) -> bool:
        """
        推送执行进度事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            progress: 进度百分比 (0-100)
            current_step: 当前步骤描述
            message: 进度消息

        Returns:
            是否推送成功
        """
        try:
            event = create_execution_progress_event(
                executionId=execution_id,
                progress=progress,
                currentStep=current_step,
                message=message,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.debug(
                "推送 execution_progress 事件 | "
                "execution_id=%s | progress=%d%% | count=%d",
                execution_id,
                progress,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 execution_progress 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    async def send_execution_done(
        self,
        user_id: str,
        execution_id: str,
        success: bool,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        summary: str | None = None,
    ) -> bool:
        """
        推送执行完成事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            success: 是否成功
            output: 输出结果
            error: 错误信息
            duration_ms: 耗时（毫秒）
            summary: 执行摘要

        Returns:
            是否推送成功
        """
        try:
            event = create_execution_done_event(
                executionId=execution_id,
                success=success,
                output=output,
                error=error,
                durationMs=duration_ms,
                summary=summary,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                "推送 execution_done 事件 | "
                "execution_id=%s | success=%s | duration=%sms | count=%d",
                execution_id,
                success,
                duration_ms,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 execution_done 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    async def send_execution_cancelled(
        self,
        user_id: str,
        execution_id: str,
        reason: str,
        cancelled_by: str | None = None,
    ) -> bool:
        """
        推送执行取消事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            reason: 取消原因
            cancelled_by: 取消者 (user/system/timeout)

        Returns:
            是否推送成功
        """
        try:
            event = create_execution_cancelled_event(
                executionId=execution_id,
                reason=reason,
                cancelledBy=cancelled_by,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.info(
                "推送 execution_cancelled 事件 | "
                "execution_id=%s | reason=%s | by=%s | count=%d",
                execution_id,
                reason,
                cancelled_by,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 execution_cancelled 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    # ========================================================================
    # 思考模式事件推送
    # ========================================================================

    async def send_thinking_start(
        self,
        user_id: str,
        execution_id: str,
        model: str | None = None,
    ) -> bool:
        """
        推送思考开始事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            model: 模型名称

        Returns:
            是否推送成功
        """
        try:
            event = create_thinking_start_event(
                executionId=execution_id,
                model=model,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.debug(
                "推送 thinking_start 事件 | execution_id=%s | model=%s | count=%d",
                execution_id,
                model,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 thinking_start 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    async def send_thinking_chunk(
        self,
        user_id: str,
        execution_id: str,
        chunk: str,
    ) -> bool:
        """
        推送思考内容片段事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            chunk: 思考内容片段

        Returns:
            是否推送成功
        """
        try:
            event = create_thinking_chunk_event(
                executionId=execution_id,
                chunk=chunk,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            # 不记录 debug 日志，避免刷屏
            return count > 0
        except Exception as e:
            logger.error(
                "推送 thinking_chunk 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    async def send_thinking_end(
        self,
        user_id: str,
        execution_id: str,
        duration_ms: int | None = None,
    ) -> bool:
        """
        推送思考结束事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            duration_ms: 思考耗时（毫秒）

        Returns:
            是否推送成功
        """
        try:
            event = create_thinking_end_event(
                executionId=execution_id,
                durationMs=duration_ms,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.debug(
                "推送 thinking_end 事件 | execution_id=%s | duration=%sms | count=%d",
                execution_id,
                duration_ms,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 thinking_end 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    # ========================================================================
    # 系统警告事件推送
    # ========================================================================

    async def send_cost_warning(
        self,
        user_id: str,
        execution_id: str,
        current_cost: float,
        threshold: float,
        message: str,
    ) -> bool:
        """
        推送成本预警事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            current_cost: 当前成本
            threshold: 阈值
            message: 警告消息

        Returns:
            是否推送成功
        """
        try:
            event = create_cost_warning_event(
                executionId=execution_id,
                currentCost=current_cost,
                threshold=threshold,
                message=message,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.warning(
                "推送 cost_warning 事件 | "
                "execution_id=%s | cost=%.2f | threshold=%.2f | count=%d",
                execution_id,
                current_cost,
                threshold,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 cost_warning 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False

    async def send_resource_limit(
        self,
        user_id: str,
        execution_id: str,
        limit_type: str,
        current: int,
        limit: int,
        message: str,
    ) -> bool:
        """
        推送资源限制事件

        Args:
            user_id: 用户 ID
            execution_id: 执行 ID
            limit_type: 限制类型 (iterations/time/tokens)
            current: 当前值
            limit: 限制值
            message: 警告消息

        Returns:
            是否推送成功
        """
        try:
            event = create_resource_limit_event(
                executionId=execution_id,
                limitType=limit_type,
                current=current,
                limit=limit,
                message=message,
            )
            count = await self.connection_manager.send_to_user(user_id, event.to_dict())
            logger.warning(
                "推送 resource_limit 事件 | "
                "execution_id=%s | type=%s | %d/%d | count=%d",
                execution_id,
                limit_type,
                current,
                limit,
                count,
            )
            return count > 0
        except Exception as e:
            logger.error(
                "推送 resource_limit 事件失败 | execution_id=%s | error=%s",
                execution_id,
                e,
            )
            return False


# 全局事件服务实例（懒加载，避免循环导入）
_event_service: WebSocketEventService | None = None


def get_event_service() -> WebSocketEventService | None:
    """获取全局事件服务实例"""
    global _event_service
    if _event_service is None:
        from src.api.websocket.handler import connection_manager

        _event_service = WebSocketEventService(connection_manager)
    return _event_service


def set_event_service(service: WebSocketEventService):
    """设置全局事件服务实例（用于测试）"""
    global _event_service
    _event_service = service
