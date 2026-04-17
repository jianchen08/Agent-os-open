"""CLI 交互通知器模块。

为 CLI 通道提供子 Agent 与人类的交互功能。
通过 rich Panel 在终端显示子 Agent 的交互请求，
并通过 asyncio.Queue 管理待处理请求。

暴露接口：
- CLIInteractionNotifier：CLI 交互通知器类
- run_sub_conversation：子对话模式异步函数
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from rich.console import Console
from rich.panel import Panel

from channels.cli.input_adapter import CLIInputAdapter

logger = logging.getLogger(__name__)


class IInteractionNotifier(ABC):
    """交互通知器接口（CLI 本地副本，避免依赖不可用的模块）。

    负责将交互请求推送到前端。
    """

    @abstractmethod
    async def notify_request(self, request: Any) -> bool:
        """通知有新的交互请求。"""
        ...

    @abstractmethod
    async def notify_cancel(
        self, request_id: str, reason: str | None = None, thread_id: str = ""
    ) -> bool:
        """通知请求已取消。"""
        ...

    @abstractmethod
    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        """通知请求已超时。"""
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
        """发送超时提醒。"""
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
        """通知对话模式开始。"""
        ...


class CLIInteractionNotifier(IInteractionNotifier):
    """CLI 交互通知器。

    将子 Agent 的交互请求通过 rich Panel 显示在终端，
    并将请求信息放入 asyncio.Queue 供主循环轮询处理。
    """

    def __init__(self, console: Console) -> None:
        """初始化 CLI 交互通知器。

        Args:
            console: rich Console 实例，用于渲染面板
        """
        self._console = console
        self._pending_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def notify_request(self, request: Any) -> bool:
        """在终端显示子 Agent 的交互请求。

        choice 模式：显示标题、描述和选项列表。
        conversation 模式：显示标题和初始消息。
        显示后将请求信息放入待处理队列。

        Args:
            request: 交互请求对象（需有 .id 和 .message_data 属性）

        Returns:
            始终返回 True
        """
        msg_data = getattr(request, "message_data", None) or {}
        if isinstance(request, dict):
            msg_data = request.get("message_data", {})

        mode = msg_data.get("interaction_mode", "choice")
        title = msg_data.get("title", "子 Agent 请求")
        description = msg_data.get("description", "")
        agent_id = msg_data.get("agent_id", "unknown")

        if mode == "choice":
            content = self._render_choice_content(description, msg_data)
            panel = Panel(
                content,
                title=f"[bold cyan]{title}[/bold cyan]",
                subtitle=f"[dim]agent: {agent_id}[/dim]",
                border_style="cyan",
            )
        else:
            initial_message = msg_data.get("initial_message", "")
            content = initial_message or description or "(对话模式)"
            panel = Panel(
                content,
                title=f"[bold green]{title}[/bold green]",
                subtitle=f"[dim]agent: {agent_id}[/dim]",
                border_style="green",
            )

        self._console.print(panel)

        request_id = getattr(request, "id", None) or (request.get("id", "") if isinstance(request, dict) else "")
        await self._pending_queue.put({
            "request_id": str(request_id),
            "message_data": msg_data,
        })

        logger.info(
            "[CLINotifier] 交互请求已显示 | request_id=%s | mode=%s",
            request_id, mode,
        )

        return True

    async def notify_cancel(
        self, request_id: str, reason: str | None = None, thread_id: str = ""
    ) -> bool:
        """打印取消通知。"""
        reason_text = f" (原因: {reason})" if reason else ""
        self._console.print(
            f"[yellow]交互请求已取消: {request_id[:12]}...{reason_text}[/yellow]"
        )
        return True

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        """打印超时通知。"""
        self._console.print(
            f"[red]交互请求已超时: {request_id[:12]}...[/red]"
        )
        return True

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
        """打印超时提醒。"""
        self._console.print(
            f"[yellow]超时提醒: 还剩 {remaining_seconds} 秒"
            f" (请求: {title or request_id[:12]})[/yellow]"
        )
        return True

    async def notify_conversation_start(
        self,
        thread_id: str,
        tab_id: str,
        title: str,
        request_id: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
    ) -> bool:
        """通知对话模式开始。CLI 模式下复用 notify_request，直接返回 True。"""
        return True

    def has_pending(self) -> bool:
        """检查是否有待处理请求。"""
        return not self._pending_queue.empty()

    def get_next_pending(self) -> dict[str, Any] | None:
        """非阻塞取出下一个待处理请求。"""
        try:
            return self._pending_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    @staticmethod
    def _render_choice_content(description: str, msg_data: dict[str, Any]) -> str:
        """渲染 choice 模式的面板内容。

        Args:
            description: 请求描述
            msg_data: 请求消息数据

        Returns:
            拼接后的面板内容字符串
        """
        options = msg_data.get("options") or []
        questions = msg_data.get("questions") or []

        content_parts: list[str] = []
        if description:
            content_parts.append(description)

        if questions:
            content_parts.append("\n[bold]问题:[/bold]")
            for i, q in enumerate(questions, 1):
                content_parts.append(f"  {i}. {q}")

        if options:
            content_parts.append("\n[bold]选项:[/bold]")
            for i, opt in enumerate(options, 1):
                opt_id = opt.get("id", str(i))
                opt_label = opt.get("label", str(opt_id))
                content_parts.append(f"  [{i}] {opt_label} (id: {opt_id})")

        return "\n".join(content_parts) if content_parts else "(无详细内容)"


async def run_sub_conversation(
    console: Console,
    input_adapter: CLIInputAdapter,
    notifier: CLIInteractionNotifier,
    interaction_service: Any,
    idle_timeout: int = 60,
) -> None:
    """处理子 Agent 的交互请求，进入子对话模式。

    从 notifier 取出 pending 请求，显示子 Agent 名称并切换提示符，
    进入循环等待用户输入，提交响应给交互服务。

    退出条件：
    - 用户超时（idle_timeout 秒无输入）
    - 用户输入 /back
    - 子 Agent 不再提问（90秒无新请求）

    Args:
        console: rich Console 实例
        input_adapter: CLI 输入适配器
        notifier: CLI 交互通知器
        interaction_service: 交互服务实例（需有 submit_response 方法）
        idle_timeout: 空闲超时秒数，默认 60
    """
    loop = asyncio.get_event_loop()

    pending = notifier.get_next_pending()
    if not pending:
        return

    msg_data = pending.get("message_data", {})
    request_id = pending.get("request_id", "")
    agent_name = msg_data.get("agent_id", "子 Agent")
    title = msg_data.get("title", "")
    mode = msg_data.get("interaction_mode", "choice")
    options = msg_data.get("options") or []

    console.print(
        f"\n[bold magenta]─── 进入 {agent_name} 对话 ───"
        f"（{idle_timeout}秒无输入自动退出）[/bold magenta]"
    )
    if title:
        console.print(f"[dim]主题: {title}[/dim]")

    original_prompt = input_adapter._prompt_str
    input_adapter._prompt_str = f"[{agent_name}] > "

    try:
        while True:
            if mode == "choice" and options:
                console.print(
                    "[dim]请输入选项编号或选项 ID (输入 /back 返回主对话):[/dim]"
                )
            elif mode == "conversation":
                console.print(
                    "[dim]请输入回复内容 (输入 /back 返回主对话):[/dim]"
                )

            try:
                user_input = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda _p=input_adapter._prompt_str: input(f"\n{_p}"),
                    ),
                    timeout=idle_timeout,
                )
            except asyncio.TimeoutError:
                console.print(
                    f"[yellow]子 Agent 对话超时 ({idle_timeout}秒无输入)[/yellow]"
                )
                break

            if user_input.strip().lower() in ("/back", "/done", "/返回"):
                console.print(
                    "[bold magenta]─── 返回主 Agent 对话 ───[/bold magenta]\n"
                )
                break

            if not user_input.strip():
                continue

            await _submit_user_response(
                interaction_service=interaction_service,
                request_id=request_id,
                mode=mode,
                user_input=user_input.strip(),
                options=options,
            )

            try:
                next_pending = await asyncio.wait_for(
                    _wait_for_next_pending(notifier),
                    timeout=90,
                )
                if next_pending is None:
                    console.print("[dim]子 Agent 不再有新的交互请求[/dim]")
                    break

                pending = next_pending
                msg_data = pending.get("message_data", {})
                request_id = pending.get("request_id", "")
                mode = msg_data.get("interaction_mode", "choice")
                title = msg_data.get("title", "")
                options = msg_data.get("options") or []

                console.print("")
                if mode == "choice":
                    content = CLIInteractionNotifier._render_choice_content(
                        msg_data.get("description", ""), msg_data,
                    )
                    panel = Panel(
                        content,
                        title=f"[bold cyan]{title}[/bold cyan]",
                        border_style="cyan",
                    )
                else:
                    initial_msg = msg_data.get("initial_message", "")
                    panel = Panel(
                        initial_msg or "(对话模式)",
                        title=f"[bold green]{title}[/bold green]",
                        border_style="green",
                    )
                console.print(panel)

            except asyncio.TimeoutError:
                console.print(
                    "[yellow]等待子 Agent 新请求超时 (90秒)[/yellow]"
                )
                break

    finally:
        input_adapter._prompt_str = original_prompt


async def _submit_user_response(
    interaction_service: Any,
    request_id: str,
    mode: str,
    user_input: str,
    options: list[dict],
) -> None:
    """提交用户响应给交互服务。

    choice 模式：尝试解析用户输入为选项 ID，匹配失败则作为 feedback。
    conversation 模式：直接作为 feedback 提交。

    Args:
        interaction_service: 交互服务实例
        request_id: 请求 ID
        mode: 交互模式 (choice/conversation)
        user_input: 用户输入的文本
        options: 选项列表
    """
    try:
        if mode == "choice":
            selected_option = _resolve_choice(user_input, options)
            if selected_option is not None:
                await interaction_service.submit_response(
                    request_id=request_id,
                    response_type="approved",
                    selected_option=selected_option,
                )
            else:
                await interaction_service.submit_response(
                    request_id=request_id,
                    response_type="answered",
                    feedback=user_input,
                )
        else:
            await interaction_service.submit_response(
                request_id=request_id,
                response_type="approved",
                feedback=user_input,
            )
    except Exception as exc:
        logger.warning(
            "[CLINotifier] 提交响应失败 | request_id=%s | error=%s",
            request_id, exc,
        )


async def _wait_for_next_pending(
    notifier: CLIInteractionNotifier,
) -> dict[str, Any] | None:
    """异步等待下一个待处理请求。

    使用短间隔轮询 notifier 的 pending 队列，
    直到有新请求或被外部超时取消。

    Args:
        notifier: CLI 交互通知器

    Returns:
        待处理请求字典，如果没有则返回 None
    """
    while True:
        pending = notifier.get_next_pending()
        if pending is not None:
            return pending
        await asyncio.sleep(0.5)


def _resolve_choice(user_input: str, options: list[dict]) -> str | None:
    """解析用户输入，匹配选项。

    支持以下输入方式：
    - 数字编号（1, 2, 3...）匹配选项位置
    - 选项 ID 精确匹配
    - 选项 label 部分匹配（不区分大小写）

    Args:
        user_input: 用户输入的文本
        options: 选项列表，每项包含 id 和 label

    Returns:
        匹配的选项 ID，无匹配返回 None
    """
    if not options or not user_input:
        return None

    if user_input.isdigit():
        index = int(user_input) - 1
        if 0 <= index < len(options):
            return options[index].get("id")

    for opt in options:
        if opt.get("id") == user_input:
            return opt.get("id")

    for opt in options:
        label = opt.get("label", "")
        if user_input.lower() in label.lower():
            return opt.get("id")

    return None
