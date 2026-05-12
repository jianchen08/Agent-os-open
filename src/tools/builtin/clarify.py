"""
澄清工具

当 Agent 在准备阶段发现信息不足时，调用此工具通知用户需要补充信息。
用户会收到通知并跳转到对应的 Agent Tab 进行对话。

行为：
1. 更新 Tab 状态为 waiting_input
2. 推送 WebSocket 事件 (clarification_needed)
3. 返回等待标记，Agent 暂停等待用户回复
"""

import logging
from typing import Any

from src.core.results import ToolExecutionResult
from src.tools.builtin.base import BuiltinTool
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_success_result,
)

logger = logging.getLogger(__name__)


class ClarifyTool(BuiltinTool):
    """
    澄清工具

    当信息不足时，通知用户需要澄清。
    用户会收到通知并跳转到对话页面。
    """

    def __init__(
        self,
        user_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        tab_id: str | None = None,
    ):
        """
        初始化澄清工具

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            task_id: 任务 ID
            tab_id: Agent Tab ID
        """
        self.user_id = user_id
        self.session_id = session_id
        self.task_id = task_id
        self.tab_id = tab_id

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="clarify",
            description="当信息不足或需求不明确时，向用户请求澄清。调用后用户会收到通知并跳转到对话页面进行交流。适用于缺少关键参数、需要确认理解、或在多个方案中需要用户选择的场景。",
            input_schema={
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要用户回答的问题列表，每个问题应清晰明确，帮助用户理解需要提供什么信息",
                    },
                    "context": {
                        "type": "string",
                        "description": "请求澄清的背景说明，解释为什么需要这些信息以及将用于什么目的",
                    },
                },
                "required": ["questions"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.ALL,
            tags=["clarification", "interaction"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        执行澄清工具

        Args:
            inputs: 输入参数
                - questions: 需要澄清的问题列表
                - context: 澄清上下文说明（可选）

        Returns:
            执行结果
        """
        questions: list[str] = inputs.get("questions", [])
        context: str | None = inputs.get("context")

        if not questions:
            return ToolExecutionResult.create_failed(
                error="questions 参数不能为空"
            )

        # 验证必要的上下文信息
        if not self.user_id:
            return ToolExecutionResult.create_failed(
                error="缺少 user_id，无法推送澄清通知"
            )

        try:
            # 1. 推送 WebSocket 事件
            event_service = self._get_event_service()
            if event_service:
                await event_service.send_clarification_needed(
                    user_id=self.user_id,
                    taskId=self.task_id or "",
                    sessionId=self.session_id or "",
                    tabId=self.tab_id or "",
                    questions=questions,
                    context=context,
                )
                logger.info(
                    f"澄清通知已推送 | user_id={self.user_id} | "
                    f"task_id={self.task_id} | questions={len(questions)}"
                )
            else:
                logger.warning("事件服务不可用，无法推送澄清通知")

            # 2. 更新任务状态（如果有 task_id）
            if self.task_id:
                await self._update_task_status("waiting_clarification")

            # 3. 返回等待标记
            return create_success_result(
                data={
                    "status": "waiting",
                    "message": "已通知用户，等待澄清回复",
                    "questions": questions,
                    "context": context,
                    "task_id": self.task_id,
                    "tab_id": self.tab_id,
                },
            )

        except Exception as e:
            logger.error(f"澄清工具执行失败: {e}", exc_info=True)
            return ToolExecutionResult.create_failed(
                error=f"澄清工具执行失败: {str(e)}"
            )

    def _get_event_service(self):
        """获取事件推送服务"""
        try:
            from src.api.websocket.service import get_event_service

            return get_event_service()
        except Exception:
            return None

    async def _update_task_status(self, status: str) -> None:
        """
        更新任务状态

        Args:
            status: 新状态
        """
        try:
            from sqlalchemy import update

            from src.db.models import Task
            from src.db.session_manager import managed_session

            # BUG-FIX-fix_20260226_async_session: 修复 async_generator 错误
            async with managed_session() as session:
                await session.execute(
                    update(Task).where(Task.id == self.task_id).values(status=status)
                )
                await session.commit()
                logger.info(f"任务 {self.task_id} 状态已更新为 {status}")
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")


def create_clarify_tool(
    user_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    tab_id: str | None = None,
) -> ClarifyTool:
    """
    创建澄清工具实例

    Args:
        user_id: 用户 ID
        session_id: 会话 ID
        task_id: 任务 ID
        tab_id: Agent Tab ID

    Returns:
        ClarifyTool 实例
    """
    return ClarifyTool(
        user_id=user_id,
        session_id=session_id,
        task_id=task_id,
        tab_id=tab_id,
    )
