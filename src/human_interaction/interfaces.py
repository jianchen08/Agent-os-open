"""
人类交互服务接口定义

暴露接口：
- IInteractionNotifier：交互通知器接口
- IHumanInteractionService：人类交互服务接口
"""

from abc import ABC, abstractmethod
from typing import Any

from human_interaction.models import Priority


class IInteractionNotifier(ABC):
    """交互通知器接口，负责将交互请求推送到前端"""

    @abstractmethod
    async def notify_request(self, request: Any) -> bool:
        """通知有新的交互请求"""
        ...

    @abstractmethod
    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        """通知请求已取消"""
        ...

    @abstractmethod
    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        """通知请求已超时"""
        ...

    @abstractmethod
    async def notify_timeout_reminder(
        self,
        request_id: str,
        remaining_seconds: int,
        thread_id: str = "",
        *,
        title: str = "",
        mode: str = "",
        options: list[dict] | None = None,
        questions: list[str] | None = None,
    ) -> bool:
        """发送超时提醒"""
        ...

    @abstractmethod
    async def notify_conversation_start(
        self,
        thread_id: str,
        tab_id: str,
        title: str,
        request_id: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
    ) -> bool:
        """通知对话模式开始"""
        ...


class IHumanInteractionService(ABC):
    """
    人类交互服务接口
    统一的人类交互抽象层，支持：
    - 选择模式：审批确认、澄清问题、方案选择
    - 对话模式：跳转到对话标签页
    - 通知模式：非阻塞推送消息到前端
    """

    @abstractmethod
    async def send_notification(
        self,
        session_id: str,
        thread_id: str,
        title: str,
        message: str = "",
        priority: Priority = Priority.NORMAL,
        progress: float | None = None,
        agent_id: str | None = None,
    ) -> str:
        """发送非阻塞通知，不等待用户响应，立即返回 request_id"""
        ...

    @abstractmethod
    async def create_choice_request(
        self,
        session_id: str,
        thread_id: str,
        tab_id: str,
        title: str,
        description: str = "",
        options: list[dict[str, Any]] | None = None,
        questions: list[str] | None = None,
        timeout_seconds: int | None = None,
        priority: Priority = Priority.NORMAL,
        user_id: str | None = None,
        agent_id: str | None = None,
        file_paths: list[str] | None = None,
        agent_level: str | None = None,
    ) -> str:
        """创建选择模式请求"""
        ...

    @abstractmethod
    async def create_conversation_request(
        self,
        session_id: str,
        thread_id: str,
        tab_id: str,
        title: str,
        description: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        file_paths: list[str] | None = None,
        agent_level: str | None = None,
    ) -> str:
        """创建对话模式请求"""
        ...

    @abstractmethod
    async def wait_for_choice(
        self,
        request_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """等待用户选择"""
        ...

    @abstractmethod
    async def submit_response(
        self,
        request_id: str,
        response_type: str,
        selected_option: str | None = None,
        answers: list[str] | None = None,
        feedback: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """提交响应"""
        ...

    @abstractmethod
    async def respond(self, request_id: str, resp_data: dict[str, Any]) -> bool:
        """处理前端交互响应，路由到 submit_response"""
        ...

    @abstractmethod
    async def mark_as_viewed(self, request_id: str) -> bool:
        """标记请求为已查看"""
        ...

    @abstractmethod
    async def cancel_request(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> bool:
        """取消请求"""
        ...

    @abstractmethod
    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        """获取请求详情"""
        ...

    @abstractmethod
    async def get_pending_requests(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取待处理请求列表"""
        ...

    @abstractmethod
    async def get_interaction_history(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """获取交互历史"""
        ...

    @abstractmethod
    async def auto_complete_conversation_for_pipeline(self, pipeline_id: str) -> int:
        """自动完成指定管道的 pending conversation 模式交互请求"""
        ...

    @abstractmethod
    async def cancel_pending_for_thread(self, thread_id: str, reason: str = "new_message_arrived") -> int:
        """取消指定 thread 关联的所有 pending 交互请求"""
        ...

    @abstractmethod
    async def wait_for_conversation_arrival(self, request_id: str, timeout: float = 86400.0) -> dict[str, Any]:
        """等待用户到达对话页面"""
        ...

    @abstractmethod
    def set_notifier(self, notifier: IInteractionNotifier) -> None:
        """设置通知器"""
        ...
