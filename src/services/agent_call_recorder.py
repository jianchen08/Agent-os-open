"""
Agent 调用记录器

在 Agent 调用开始/结束时自动记录到数据库
"""

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.repositories.agent_call_repository import AgentCallRepository

logger = logging.getLogger(__name__)


class AgentCallRecorder:
    """
    Agent 调用记录器

    提供便捷的方法在 Agent 调用时记录执行信息
    """

    def __init__(self, session: AsyncSession):
        """
        初始化记录器

        Args:
            session: 数据库会话
        """
        self.session = session
        self.repository = AgentCallRepository(session)

    async def start_call(
        self,
        caller_level: str,
        target_agent_id: str,
        target_agent_name: str,
        operation_type: str,
        instruction: str,
        instruction_summary: str | None = None,
        context: dict[str, Any] | None = None,
        timeout: int = 300,
        retry_count: int = 1,
        priority: str = "normal",
    ) -> str:
        """
        记录调用开始

        Args:
            caller_level: 调用者层级 (L1/L2)
            target_agent_id: 目标 Agent ID
            target_agent_name: 目标 Agent 名称
            operation_type: 操作类型 (task_submit/agent_call 等)
            instruction: 完整指令
            instruction_summary: 指令摘要（可选，默认截取前 100 字符）
            context: 上下文信息
            timeout: 超时时间
            retry_count: 重试次数
            priority: 优先级

        Returns:
            execution_id: 执行 ID，用于后续更新状态
        """
        execution_id = str(uuid.uuid4())

        # 生成摘要
        if not instruction_summary:
            instruction_summary = (
                instruction[:100] + "..." if len(instruction) > 100 else instruction
            )

        try:
            await self.repository.create(
                {
                    "execution_id": execution_id,
                    "caller_level": caller_level,
                    "target_agent_id": target_agent_id,
                    "target_agent_name": target_agent_name,
                    "operation_type": operation_type,
                    "instruction": instruction,
                    "instruction_summary": instruction_summary,
                    "context": context,
                    "timeout": timeout,
                    "retry_count": retry_count,
                    "priority": priority,
                }
            )
            await self.session.commit()

            logger.info(
                "Agent 调用记录已创建 | execution_id=%s | "
                "caller=%s | target=%s | operation=%s",
                execution_id,
                caller_level,
                target_agent_name,
                operation_type,
            )

            return execution_id

        except Exception as e:
            logger.error("创建 Agent 调用记录失败: %s", e)
            await self.session.rollback()
            raise

    async def update_running(self, execution_id: str) -> bool:
        """
        更新状态为运行中

        Args:
            execution_id: 执行 ID

        Returns:
            是否更新成功
        """
        try:
            success = await self.repository.update_status(execution_id, "running")
            await self.session.commit()
            return success
        except Exception as e:
            logger.error("更新 Agent 调用状态失败: %s", e)
            await self.session.rollback()
            return False

    async def complete_call(
        self,
        execution_id: str,
        success: bool,
        result: dict[str, Any] | None = None,
        result_summary: str | None = None,
    ) -> bool:
        """
        记录调用完成

        Args:
            execution_id: 执行 ID
            success: 是否成功
            result: 执行结果
            result_summary: 结果摘要

        Returns:
            是否更新成功
        """
        try:
            success_flag = await self.repository.complete(
                execution_id=execution_id,
                success=success,
                result=result,
                result_summary=result_summary,
            )
            await self.session.commit()

            logger.info(
                "Agent 调用已完成 | execution_id=%s | success=%s",
                execution_id,
                success,
            )

            return success_flag

        except Exception as e:
            logger.error("完成 Agent 调用记录失败: %s", e)
            await self.session.rollback()
            return False

    async def fail_call(
        self,
        execution_id: str,
        error: str,
    ) -> bool:
        """
        记录调用失败

        Args:
            execution_id: 执行 ID
            error: 错误信息

        Returns:
            是否更新成功
        """
        try:
            success = await self.repository.fail(
                execution_id=execution_id,
                error=error,
            )
            await self.session.commit()

            logger.warning(
                "Agent 调用失败 | execution_id=%s | error=%s",
                execution_id,
                error,
            )

            return success

        except Exception as e:
            logger.error("记录 Agent 调用失败状态失败: %s", e)
            await self.session.rollback()
            return False

    @asynccontextmanager
    async def record_call(
        self,
        caller_level: str,
        target_agent_id: str,
        target_agent_name: str,
        operation_type: str,
        instruction: str,
        instruction_summary: str | None = None,
        context: dict[str, Any] | None = None,
        timeout: int = 300,
        retry_count: int = 1,
        priority: str = "normal",
    ):
        """
        上下文管理器：自动记录调用开始和结束

        使用示例:
            async with recorder.record_call(...) as execution_id:
                # 执行 Agent 调用
                result = await agent.run(...)
                # 设置结果（可选）
                recorder.set_result(result)

        Args:
            caller_level: 调用者层级
            target_agent_id: 目标 Agent ID
            target_agent_name: 目标 Agent 名称
            operation_type: 操作类型
            instruction: 指令
            instruction_summary: 指令摘要
            context: 上下文
            timeout: 超时时间
            retry_count: 重试次数
            priority: 优先级

        Yields:
            execution_id: 执行 ID
        """
        execution_id = await self.start_call(
            caller_level=caller_level,
            target_agent_id=target_agent_id,
            target_agent_name=target_agent_name,
            operation_type=operation_type,
            instruction=instruction,
            instruction_summary=instruction_summary,
            context=context,
            timeout=timeout,
            retry_count=retry_count,
            priority=priority,
        )

        # 更新为运行中
        await self.update_running(execution_id)

        # 用于存储结果
        self._current_result: dict[str, Any] | None = None
        self._current_result_summary: str | None = None

        try:
            yield execution_id
            # 正常完成
            await self.complete_call(
                execution_id=execution_id,
                success=True,
                result=self._current_result,
                result_summary=self._current_result_summary,
            )
        except Exception as e:
            # 调用失败
            await self.fail_call(
                execution_id=execution_id,
                error=str(e),
            )
            raise

    def set_result(
        self,
        result: dict[str, Any] | None = None,
        result_summary: str | None = None,
    ):
        """
        设置当前调用的结果（在 record_call 上下文中使用）

        Args:
            result: 执行结果
            result_summary: 结果摘要
        """
        self._current_result = result
        self._current_result_summary = result_summary


# 全局记录器工厂
_recorder_cache: dict[int, AgentCallRecorder] = {}


def get_agent_call_recorder(session: AsyncSession) -> AgentCallRecorder:
    """
    获取 Agent 调用记录器

    Args:
        session: 数据库会话

    Returns:
        AgentCallRecorder 实例
    """
    session_id = id(session)
    if session_id not in _recorder_cache:
        _recorder_cache[session_id] = AgentCallRecorder(session)
    return _recorder_cache[session_id]
