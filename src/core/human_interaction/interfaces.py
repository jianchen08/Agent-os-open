"""
人类交互服务接口定义

定义统一的人类交互抽象接口
"""

from abc import ABC, abstractmethod
from typing import Any

from src.core.human_interaction.models import (
    InteractionRequest,
    InteractionResponse,
    InteractionStatus,
    InteractionType,
    Priority,
)


class IInteractionNotifier(ABC):
    """
    交互通知器接口

    负责将交互请求推送到前端或其他系统
    """

    @abstractmethod
    async def notify_request(self, request: InteractionRequest) -> bool:
        """
        通知有新的交互请求

        Args:
            request: 交互请求

        Returns:
            是否通知成功
        """
        ...

    @abstractmethod
    async def notify_cancel(self, request_id: str, reason: str | None = None) -> bool:
        """
        通知请求已取消

        Args:
            request_id: 请求 ID
            reason: 取消原因

        Returns:
            是否通知成功
        """
        ...

    @abstractmethod
    async def notify_timeout(self, request_id: str) -> bool:
        """
        通知请求已超时

        Args:
            request_id: 请求 ID

        Returns:
            是否通知成功
        """
        ...


class IHumanInteractionService(ABC):
    """
    人类交互服务接口

    统一的人类交互抽象层，支持：
    - 审批模式：传统的审批流程
    - 对话模式：直接与 Agent 对话

    所有需要人类参与的场景都通过此接口统一处理。
    """

    @abstractmethod
    async def request_interaction(
        self,
        request: InteractionRequest,
    ) -> str:
        """
        发起交互请求

        Args:
            request: 交互请求对象

        Returns:
            请求 ID
        """
        ...

    @abstractmethod
    async def wait_for_response(
        self,
        request_id: str,
        timeout: float | None = None,
    ) -> InteractionResponse:
        """
        等待用户响应

        Args:
            request_id: 请求 ID
            timeout: 超时时间（秒），None 使用请求中的超时设置

        Returns:
            交互响应

        Raises:
            InteractionTimeoutError: 交互超时
            InteractionCancelledError: 交互被取消
            InteractionDeniedError: 交互被拒绝
        """
        ...

    @abstractmethod
    async def submit_response(
        self,
        request_id: str,
        response: InteractionResponse,
    ) -> bool:
        """
        提交响应

        Args:
            request_id: 请求 ID
            response: 交互响应

        Returns:
            是否提交成功
        """
        ...

    @abstractmethod
    async def cancel_request(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> bool:
        """
        取消请求

        Args:
            request_id: 请求 ID
            reason: 取消原因

        Returns:
            是否取消成功
        """
        ...

    @abstractmethod
    async def get_request(self, request_id: str) -> InteractionRequest | None:
        """
        获取请求详情

        Args:
            request_id: 请求 ID

        Returns:
            请求对象，不存在返回 None
        """
        ...

    @abstractmethod
    async def get_request_status(self, request_id: str) -> InteractionStatus | None:
        """
        获取请求状态

        Args:
            request_id: 请求 ID

        Returns:
            请求状态，不存在返回 None
        """
        ...

    @abstractmethod
    async def get_pending_requests(
        self,
        thread_id: str | None = None,
        agent_id: str | None = None,
        priority: Priority | None = None,
        interaction_type: InteractionType | None = None,
        limit: int = 50,
    ) -> list[InteractionRequest]:
        """
        获取待处理请求列表

        Args:
            thread_id: 线程 ID 过滤
            agent_id: Agent ID 过滤
            priority: 优先级过滤
            interaction_type: 交互类型过滤
            limit: 返回数量限制

        Returns:
            待处理请求列表
        """
        ...

    @abstractmethod
    async def approve(
        self,
        request_id: str,
        option_id: str | None = None,
        reason: str | None = None,
        user_id: str | None = None,
    ) -> InteractionResponse:
        """
        批准请求（便捷方法）

        Args:
            request_id: 请求 ID
            option_id: 选择的选项 ID
            reason: 批准原因
            user_id: 用户 ID

        Returns:
            交互响应
        """
        ...

    @abstractmethod
    async def deny(
        self,
        request_id: str,
        reason: str | None = None,
        user_id: str | None = None,
    ) -> InteractionResponse:
        """
        拒绝请求（便捷方法）

        Args:
            request_id: 请求 ID
            reason: 拒绝原因
            user_id: 用户 ID

        Returns:
            交互响应
        """
        ...

    @abstractmethod
    async def modify_and_approve(
        self,
        request_id: str,
        modified_data: dict[str, Any],
        reason: str | None = None,
        user_id: str | None = None,
    ) -> InteractionResponse:
        """
        修改参数后批准（便捷方法）

        Args:
            request_id: 请求 ID
            modified_data: 修改后的数据
            reason: 修改原因
            user_id: 用户 ID

        Returns:
            交互响应
        """
        ...

    @abstractmethod
    async def end_conversation(
        self,
        request_id: str,
        result: str,
        user_id: str | None = None,
    ) -> InteractionResponse:
        """
        结束对话（便捷方法）

        Args:
            request_id: 请求 ID
            result: 对话结论
            user_id: 用户 ID

        Returns:
            交互响应
        """
        ...

    @abstractmethod
    def set_notifier(self, notifier: IInteractionNotifier) -> None:
        """
        设置通知器

        Args:
            notifier: 交互通知器
        """
        ...

    @abstractmethod
    async def should_auto_approve(
        self,
        operation: str,
        risk_level: int,
    ) -> bool:
        """
        判断是否可以自动审批

        Args:
            operation: 操作名称
            risk_level: 风险等级

        Returns:
            是否可以自动审批
        """
        ...

    @abstractmethod
    def configure_auto_approval(
        self,
        whitelist: list[str] | None = None,
        blacklist: list[str] | None = None,
        threshold: int | None = None,
    ) -> None:
        """
        配置自动审批规则

        Args:
            whitelist: 白名单操作列表
            blacklist: 黑名单操作列表
            threshold: 自动审批风险阈值
        """
        ...
