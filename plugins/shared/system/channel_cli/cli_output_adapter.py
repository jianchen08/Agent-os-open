"""CLI 输出适配器模块（Claude Code 风格）。

使用 rich 库将管道结果以彩色格式输出到终端，支持：
- 工具调用可视化：[tool] 调用 tool_name(...) → result
- 任务创建显示：[task] 创建任务 #123: 描述
- 迭代进度显示：>> 迭代 3/20
- 思考过程折叠（<think/> 过滤，可开关）
- 底部状态栏：Agent 名称、模型、轮次、上下文占用
- 模式标签：[NORMAL] [AUTO] [PLAN]
- 错误/警告/系统消息样式化
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from output_adapter import IOutputAdapter
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.text import Text

logger = logging.getLogger(__name__)


def sanitize_for_terminal(text: str) -> str:
    """清理文本中终端不兼容的字符。

    根据 stdout 实际编码检测是否需要替换 Unicode 字符。
    如果终端编码为 UTF-8，直接通过所有字符（包括 emoji）。
    如果终端编码为 GBK 等有限编码，替换不兼容字符为 ?。
    """
    import sys  # noqa: PLC0415

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    normalized = encoding.lower().replace("-", "").replace("_", "")
    # UTF-8 / cp65001 (Windows UTF-8 codepage) 终端可以直接输出所有 Unicode
    if normalized in ("utf8", "utf_8", "cp65001", "65001"):
        return text

    try:
        text.encode(encoding)
        return text
    except (UnicodeEncodeError, LookupError):
        result = []
        for ch in text:
            try:
                ch.encode(encoding)
                result.append(ch)
            except (UnicodeEncodeError, LookupError):
                result.append("?")
        return "".join(result)


# ---------------------------------------------------------------------------
# 状态栏渲染器
# ---------------------------------------------------------------------------


@dataclass
class StatusBarState:
    """状态栏数据（单一事实源，渲染器只读）。

    渠道运行面直接改字段后由 StatusBarRenderer.render() 出图，
    不再保留逐字段的 partial-update 转发样板。
    """

    agent_name: str = "Agent OS"
    model_name: str = "unknown"
    turn_count: int = 0
    context_pct: float = 0.0
    mode: str = "normal"
    task_count: int = 0
    is_processing: bool = False
    pipeline_iteration: int = 0
    pipeline_max_iterations: int = 0
    pipeline_running: bool = False
    running_task_count: int = 0
    pending_task_count: int = 0
    completed_task_count: int = 0
    failed_task_count: int = 0


class StatusBarRenderer:
    """底部状态栏渲染器。

    渲染一行状态信息，包含：Agent 名称、模型、对话轮次、
    上下文占用百分比、交互模式。

    Example::

        renderer = StatusBarRenderer()
        renderer.state.agent_name = "灵汐"
        renderer.state.mode = "auto"
        status_text = renderer.render()
    """

    def __init__(self, state: StatusBarState | None = None) -> None:
        """初始化状态栏渲染器。

        Args:
            state: 初始状态；默认创建全默认值状态。
        """
        self.state = state if state is not None else StatusBarState()

    def render(self) -> Text:  # noqa: PLR0912,PLR0915
        """渲染状态栏文本。

        左侧显示：模式标签、Agent名称、模型、轮次、上下文占用。
        右侧显示：任务状态统计、管道循环状态。

        Returns:
            rich Text 对象
        """
        s = self.state
        left_parts: list[tuple[str, str]] = []
        right_parts: list[tuple[str, str]] = []

        # --- 左侧 ---
        mode_styles = {
            "normal": "bold white",
            "auto": "bold green",
            "plan": "bold yellow",
        }
        mode_label = s.mode.upper()
        left_parts.append((f" [{mode_label}]", mode_styles.get(s.mode, "white")))

        left_parts.append((f" {s.agent_name}", "bold cyan"))

        if s.model_name and s.model_name != "unknown":
            model_short = s.model_name.split("/")[-1] if "/" in s.model_name else s.model_name
            left_parts.append((f" . {model_short}", "dim"))

        if s.turn_count > 0:
            left_parts.append((f" . 轮次 {s.turn_count}", "dim"))

        ctx_color = "green" if s.context_pct < 50 else ("yellow" if s.context_pct < 80 else "red")
        left_parts.append((f" . ctx {s.context_pct:.0f}%", ctx_color))

        if s.task_count > 0:
            left_parts.append((f" . [task]{s.task_count}", "dim"))

        if s.is_processing:
            left_parts.append((" . ...", "bold yellow"))

        # --- 右侧：任务状态 ---
        task_parts = []
        if s.running_task_count > 0:
            task_parts.append((f"run:{s.running_task_count}", "bold yellow"))
        if s.pending_task_count > 0:
            task_parts.append((f"pend:{s.pending_task_count}", "dim"))
        if s.completed_task_count > 0:
            task_parts.append((f"done:{s.completed_task_count}", "green"))
        if s.failed_task_count > 0:
            task_parts.append((f"fail:{s.failed_task_count}", "red"))
        if task_parts:
            right_parts.append(("tasks [", "dim"))
            for i, (text, style) in enumerate(task_parts):
                if i > 0:
                    right_parts.append(("|", "dim"))
                right_parts.append((text, style))
            right_parts.append(("]", "dim"))

        # --- 右侧：管道循环状态 ---
        if s.pipeline_running and s.pipeline_iteration > 0:
            iter_text = f"loop {s.pipeline_iteration}"
            if s.pipeline_max_iterations > 0:
                iter_text += f"/{s.pipeline_max_iterations}"
            right_parts.append((f" [{iter_text}]", "bold magenta"))

        # 构建带右对齐的完整行
        try:
            from shutil import get_terminal_size  # noqa: PLC0415

            term_width = get_terminal_size().columns
            if term_width < 40:
                term_width = 80
        except Exception:
            term_width = 80

        left_text = Text()
        for content, style in left_parts:
            left_text.append(content, style=style)

        right_text = Text()
        for content, style in right_parts:
            right_text.append(content, style=style)

        right_width = right_text.cell_len
        padding_needed = max(2, term_width - left_text.cell_len - right_width - 2)

        full_text = Text()
        full_text.append_text(left_text)
        full_text.append(" " * padding_needed)
        full_text.append_text(right_text)

        return full_text

    def render_simple(self) -> str:
        """渲染纯文本状态栏（用于 input 提示符）。

        Returns:
            状态栏字符串
        """
        mode_label = self.state.mode.upper()
        return f"[{mode_label}] {self.state.agent_name}"


# ---------------------------------------------------------------------------
# CLI 输出适配器
# ---------------------------------------------------------------------------


class CLIOutputAdapter(IOutputAdapter):
    """命令行输出适配器（Claude Code 风格）。

    使用 rich Console 实现增强的终端输出。支持：
    - 工具调用可视化
    - 任务创建/完成通知
    - 迭代进度显示
    - 思考过程折叠
    - 底部状态栏
    - 流式逐 token 输出
    - 错误/系统消息样式化

    Example::

        adapter = CLIOutputAdapter()
        await adapter.send({"raw_result": "Hello!", "should_stop": False})
        adapter.show_tool_call("current_time", {"timezone": "local"}, "2026-04-12 14:30:00")
    """

    def __init__(self, console: Console | None = None) -> None:
        """初始化 CLI 输出适配器。

        Args:
            console: rich Console 实例；默认创建新实例。
        """
        if console is not None:
            self._console = console
        else:
            try:
                from shutil import get_terminal_size  # noqa: PLC0415

                detected_width = get_terminal_size().columns
                width = detected_width if detected_width >= 40 else 80
            except Exception:
                width = 80
            self._console = Console(
                width=width,
            )
        self._status_bar = StatusBarRenderer()
        self._show_thinking: bool = False

    @property
    def status_bar(self) -> StatusBarRenderer:
        """获取状态栏渲染器。"""
        return self._status_bar

    @property
    def console(self) -> Console:
        """获取 rich Console 实例。"""
        return self._console

    @property
    def show_thinking(self) -> bool:
        """是否显示思考过程。"""
        return self._show_thinking

    @show_thinking.setter
    def show_thinking(self, value: bool) -> None:
        """设置是否显示思考过程。"""
        self._show_thinking = value

    async def send(self, state: dict[str, Any], streamed: bool = False) -> None:
        """输出管道最终 state。

        根据 state 内容选择输出样式：
        - 包含 error → 红色错误输出
        - should_stop == True → 蓝色系统消息
        - 正常结果 → 格式化输出（流式模式下不重复打印 raw_result）

        Args:
            state: 管道引擎的最终 state 字典。
            streamed: 是否为流式模式。流式模式下 raw_result 已通过
                on_chunk 回调实时输出，此处不再重复打印。
        """
        # 错误输出
        if error := state.get("error"):
            self._console.print(
                Panel(
                    str(error),
                    title="[bold red]错误[/bold red]",
                    border_style="red",
                    expand=False,
                )
            )
            return

        # 停止信号
        if state.get("should_stop"):
            self._console.print(Text("[系统] 会话结束", style="bold blue"))
            return

        # 正常结果输出：流式模式下不重复打印
        if streamed:
            # 换行收尾已由 on_chunk 回调中的 _text_streaming_active 逻辑处理
            # 如有 raw_error，仍然输出
            raw_error = state.get("raw_error")
            if raw_error:
                self._console.print(
                    Panel(
                        str(raw_error),
                        title="[bold yellow]警告[/bold yellow]",
                        border_style="yellow",
                        expand=False,
                    )
                )
            return

        # 非流式模式：正常输出 raw_result
        raw_result = state.get("raw_result", "")
        if raw_result:
            # Windows GBK 兼容：替换 LLM 输出中的 emoji
            safe_result = sanitize_for_terminal(str(raw_result))
            self._console.print(safe_result)

    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出一个 chunk。

        根据 chunk 类型选择输出样式：
        - type="token" → 默认颜色逐字输出（不换行）
        - type="error" → 红色输出
        - type="system" → 蓝色输出
        - type="tool_call" → 工具调用可视化
        - type="tool_result" → 工具结果
        - type="task" → 任务通知

        Args:
            chunk: 流式数据块，包含 text 和 type 字段。
        """
        text = chunk.get("text", "")
        chunk_type = chunk.get("type", "token")

        if not text and chunk_type == "token":
            return

        if chunk_type == "error":
            self._console.print(Text(text, style="red"), end="")
        elif chunk_type == "system":
            self._console.print(Text(text, style="blue"), end="")
        elif chunk_type == "tool_call":
            tool_name = chunk.get("tool_name", "unknown")
            tool_args = chunk.get("tool_args", {})
            self.show_tool_call(tool_name, tool_args)
        elif chunk_type == "tool_result":
            tool_name = chunk.get("tool_name", "unknown")
            result_text = chunk.get("result", "")
            self.show_tool_result(tool_name, result_text)
        elif chunk_type == "task":
            self.show_task_notification(chunk.get("task_action", ""), chunk.get("task_info", {}))
        elif chunk_type == "iteration":
            iteration = chunk.get("iteration", 0)
            max_iter = chunk.get("max_iterations", 0)
            self.show_iteration(iteration, max_iter)
        else:
            # 默认 token 流式输出
            self._console.print(Text(text), end="")

    # --- Claude Code 风格输出方法 ---

    def show_tool_call(self, tool_name: str, args: dict[str, Any] | None = None, pending: bool = False) -> None:
        """显示工具调用信息。

        Args:
            tool_name: 工具名称
            args: 工具参数
            pending: 是否等待确认（Auto 模式不需要确认）
        """
        # 精简参数显示
        args_str = ""
        if args:
            display_args = {k: v for k, v in args.items() if not k.startswith("_")}
            if display_args:
                items = [f"{k}={_truncate(v)}" for k, v in list(display_args.items())[:3]]
                args_str = ", ".join(items)
                if len(display_args) > 3:
                    args_str += ", ..."

        if pending:
            self._console.print(f"  [bold][tool] 调用 {tool_name}({args_str})[/bold] [yellow]>> 等待确认[/yellow]")
        else:
            self._console.print(f"  [dim][tool] 调用 {tool_name}({args_str})[/dim]")

    def show_tool_result(self, tool_name: str, result: str, success: bool = True, duration_ms: float = 0) -> None:
        """显示工具调用结果。

        Args:
            tool_name: 工具名称
            result: 结果文本
            success: 是否成功
            duration_ms: 执行耗时（毫秒）
        """
        truncated = result[:100] + "..." if len(result) > 100 else result
        icon = "OK" if success else "FAIL"
        color = "green" if success else "red"
        duration_str = f" ({duration_ms:.0f}ms)" if duration_ms else ""
        self._console.print(f"  [{color}]{icon}{duration_str} -> {truncated}[/{color}]")

    def show_task_notification(self, action: str, info: dict[str, Any]) -> None:
        """显示任务通知。

        Args:
            action: 任务动作 (created/completed/failed)
            info: 任务信息
        """
        task_id = info.get("task_id", info.get("id", "?"))
        desc = info.get("description", info.get("title", ""))

        if action == "created":
            self._console.print(f"  [cyan][task] 创建任务 #{task_id}:[/cyan] {_truncate(desc, 50)}")
        elif action == "completed":
            self._console.print(f"  [green][OK] 任务 #{task_id} 完成[/green]")
        elif action == "failed":
            self._console.print(f"  [red][FAIL] 任务 #{task_id} 失败[/red]")
        else:
            self._console.print(f"  [dim][task] 任务 #{task_id}: {action}[/dim]")

    def show_iteration(self, iteration: int, max_iterations: int) -> None:
        """显示迭代进度。

        Args:
            iteration: 当前迭代次数
            max_iterations: 最大迭代次数
        """
        self._console.print(f"  [dim]>> 迭代 {iteration}/{max_iterations}[/dim]")

    def show_system_message(self, message: str, style: str = "blue") -> None:
        """显示系统消息。

        Args:
            message: 消息内容
            style: rich 样式
        """
        self._console.print(f"[{style}][系统] {message}[/{style}]")

    def show_startup_banner(self, agent_name: str, mode: str = "normal") -> None:
        """显示启动横幅。

        Args:
            agent_name: Agent 显示名称
            mode: 交互模式
        """
        self._console.print(
            Panel(
                f"[bold cyan]{agent_name}[/bold cyan] CLI 已启动\n\n"
                f"[dim]输入消息开始对话，输入 [bold]/help[/bold] 查看命令[/dim]\n"
                f"[dim]模式: [bold]{mode.upper()}[/bold]  |  "
                f"快捷: @file  !cmd  #memo[/dim]",
                title="> Agent OS",
                border_style="cyan",
                expand=False,
            )
        )

    def show_processing(self, message: str = "思考中") -> Status:
        """创建处理中状态指示器。

        Args:
            message: 状态消息

        Returns:
            rich Status 对象，需在 with 块中使用
        """
        return Status(f"[bold yellow]>> {message}...[/bold yellow]", console=self._console)

    def show_tool_confirmation(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> str | None:
        """显示工具调用确认提示。

        Args:
            tool_name: 工具名称
            args: 工具参数

        Returns:
            用户输入的确认结果，None 表示跳过
        """
        self.show_tool_call(tool_name, args, pending=True)
        try:
            response = input("  确认执行? [Y/n/s(kip)] ").strip().lower()
            if response in ("n", "no", "skip", "s"):
                return None
            return "yes"
        except (EOFError, KeyboardInterrupt):
            return None

    def render_status_bar(self) -> None:
        """渲染状态栏到控制台。"""
        self._console.print(self._status_bar.render())


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _truncate(value: Any, max_len: int = 30) -> str:
    """截断值用于显示。

    Args:
        value: 要显示的值（非字符串值先 str 化）
        max_len: 最大长度

    Returns:
        截断后的字符串
    """
    s = str(value)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s
