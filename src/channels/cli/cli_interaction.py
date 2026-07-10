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
    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
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
        self._request_event: asyncio.Event = asyncio.Event()

    async def notify_request(self, request: Any) -> bool:
        """接收交互请求并入队。

        不打印任何提示，完整面板由 run_sub_conversation()
        在主循环检测到 pending 后统一显示。

        Args:
            request: 交互请求对象（需有 .id 和 .message_data 属性）

        Returns:
            始终返回 True
        """
        msg_data = getattr(request, "message_data", None) or {}
        if isinstance(request, dict):
            msg_data = request.get("message_data", {})

        mode = msg_data.get("interaction_mode", "choice")
        request_id = getattr(request, "id", None) or (request.get("id", "") if isinstance(request, dict) else "")

        await self._pending_queue.put(
            {
                "request_id": str(request_id),
                "message_data": msg_data,
            }
        )
        self._request_event.set()

        logger.info(
            "[CLINotifier] 交互请求已入队 | request_id=%s | mode=%s",
            request_id,
            mode,
        )

        return True

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        """打印取消通知。"""
        reason_text = f" (原因: {reason})" if reason else ""
        self._console.print(f"[yellow]交互请求已取消: {request_id[:12]}...{reason_text}[/yellow]")
        return True

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        """打印超时通知。"""
        self._console.print(f"[red]交互请求已超时: {request_id[:12]}...[/red]")
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
            f"[yellow]超时提醒: 还剩 {remaining_seconds} 秒 (请求: {title or request_id[:12]})[/yellow]"
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
        """非阻塞取出下一个待处理请求。

        取出后如果队列已空则清除事件标志，
        避免主循环重复触发。
        """
        try:
            item = self._pending_queue.get_nowait()
        except asyncio.QueueEmpty:
            self._request_event.clear()
            return None
        if self._pending_queue.empty():
            self._request_event.clear()
        return item

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


def _show_request_panel(
    console: Console,
    title: str,
    agent_name: str,
    mode: str,
    msg_data: dict[str, Any],
) -> None:
    """显示交互请求的面板。"""
    description = msg_data.get("description", "")
    if mode == "choice":
        content = CLIInteractionNotifier._render_choice_content(description, msg_data)
        panel = Panel(
            content,
            title=f"[bold cyan]{title}[/bold cyan]",
            subtitle=f"[dim]agent: {agent_name}[/dim]",
            border_style="cyan",
        )
    else:
        initial_message = msg_data.get("initial_message", "")
        content = initial_message or description or "(对话模式)"
        panel = Panel(
            content,
            title=f"[bold green]{title}[/bold green]",
            subtitle=f"[dim]agent: {agent_name}[/dim]",
            border_style="green",
        )
    console.print(panel)


async def run_sub_conversation(  # noqa: PLR0915
    console: Console,
    input_adapter: CLIInputAdapter,
    notifier: CLIInteractionNotifier,
    interaction_service: Any,
    idle_timeout: int = 86400,
) -> None:
    """处理子 Agent 的交互请求，进入子对话模式。

    通过 run_in_executor 调用 input()，避免阻塞事件循环。

    退出条件：
    - 用户输入 /back
    - 子 Agent 不再提问

    Args:
        console: rich Console 实例
        input_adapter: CLI 输入适配器
        notifier: CLI 交互通知器
        interaction_service: 交互服务实例（需有 submit_response 方法）
        idle_timeout: 未使用，保留接口兼容
    """

    pending = await _get_valid_pending(notifier, interaction_service)
    if not pending:
        return

    msg_data = pending.get("message_data", {})
    request_id = pending.get("request_id", "")
    agent_name = msg_data.get("agent_id", "子 Agent")
    title = msg_data.get("title", "")
    mode = msg_data.get("interaction_mode", "choice")
    options = msg_data.get("options") or []

    console.print("\n[bold magenta]────────────────────────────────────────────────────────[/bold magenta]")
    console.print(f"[bold magenta]  {agent_name} 请求交互（输入 /back 返回主对话）[/bold magenta]")
    console.print("[bold magenta]────────────────────────────────────────────────────────[/bold magenta]")

    _show_request_panel(console, title, agent_name, mode, msg_data)

    original_prompt = input_adapter._prompt_str
    input_adapter._prompt_str = f"[{agent_name}] > "

    async def _read_input(prompt: str) -> str:
        """通过 input_adapter 的队列读取输入。

        使用 run_in_executor 读取 stdin，保持事件循环响应。
        管道输出由 CLI 的 _suppress_streaming 标志抑制。
        """
        console.print(prompt, end="")
        reader = input_adapter._get_stdin_reader()
        loop = asyncio.get_running_loop()
        line = await loop.run_in_executor(None, reader.read_line_blocking)
        if line is None:
            raise EOFError
        return line

    try:
        while True:
            if mode == "choice" and options:
                console.print("[dim]请输入选项编号或选项 ID (输入 /back 返回主对话):[/dim]")
            elif mode == "conversation":
                console.print("[dim]请输入回复内容 (输入 /back 返回主对话):[/dim]")

            user_input = await _read_input(f"\n{input_adapter._prompt_str}")

            if user_input.strip().lower() in ("/back", "/done", "/返回"):
                # 清空剩余待处理请求，防止主循环立即重入子对话
                while notifier.get_next_pending() is not None:
                    pass
                console.print("[bold magenta]──────────────────────────────────────────────────[/bold magenta]")
                console.print("[bold magenta]  返回主 Agent 对话[/bold magenta]")
                console.print("[bold magenta]──────────────────────────────────────────────────[/bold magenta]\n")
                break

            if not user_input.strip():
                if mode == "choice" and options:
                    opt_ids = ", ".join(o.get("id", "") for o in options)
                    console.print(f"[yellow]请输入选项编号 (如 1) 或选项 ID (可选: {opt_ids})[/yellow]")
                continue

            await _submit_user_response(
                interaction_service=interaction_service,
                request_id=request_id,
                mode=mode,
                user_input=user_input.strip(),
                options=options,
            )

            # 提交后短暂等待，检查队列是否有后续请求
            await asyncio.sleep(0.5)
            next_pending = await _get_valid_pending(
                notifier,
                interaction_service,
            )
            if next_pending is None:
                console.print("[bold magenta]──────────────────────────────────────────────────[/bold magenta]")
                console.print("[bold magenta]  返回主 Agent 对话[/bold magenta]")
                console.print("[bold magenta]──────────────────────────────────────────────────[/bold magenta]\n")
                break

            pending = next_pending
            msg_data = pending.get("message_data", {})
            request_id = pending.get("request_id", "")
            mode = msg_data.get("interaction_mode", "choice")
            title = msg_data.get("title", "")
            options = msg_data.get("options") or []

            console.print("")
            _show_request_panel(console, title, agent_name, mode, msg_data)

    except (EOFError, asyncio.CancelledError):
        console.print("\n[dim yellow]stdin 已关闭，退出子对话[/dim yellow]")

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
            request_id,
            exc,
        )


def _resolve_choice(user_input: str, options: list[dict]) -> str | None:
    """解析用户输入，匹配选项。

    仅支持数字编号（1, 2, 3...）匹配选项位置。
    非数字输入或无效编号将返回 None，由调用方将原始输入作为 feedback 返回。

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

    return None


async def _get_valid_pending(
    notifier: CLIInteractionNotifier,
    interaction_service: Any,
) -> dict[str, Any] | None:
    """从通知器队列中取出下一个仍然有效的请求。

    跳过已被取消、超时或已完成的请求（管道取消时
    工具会调用 cancel_request 清理，但通知器队列中
    残留的条目不会被自动移除）。

    Args:
        notifier: CLI 交互通知器
        interaction_service: 交互服务实例

    Returns:
        第一个仍然有效的请求，或 None
    """
    while True:
        pending = notifier.get_next_pending()
        if pending is None:
            return None
        request_id = pending.get("request_id", "")
        if interaction_service and request_id:
            try:
                record = await interaction_service.get_request(
                    request_id,
                )
                if record and record.get("status") == "pending":
                    return pending
                logger.debug(
                    "[CLINotifier] 跳过已失效请求 | request_id=%s | status=%s",
                    request_id,
                    record.get("status", "?") if record else "gone",
                )
            except Exception:
                return pending
        else:
            return pending
