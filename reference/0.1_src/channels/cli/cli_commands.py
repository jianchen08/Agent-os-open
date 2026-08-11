"""CLI 斜杠命令处理器模块。

提供 Claude Code 风格的斜杠命令系统，支持：

- /help   — 显示命令列表
- /compact — 上下文压缩
- /clear  — 清空对话历史
- /model  — 查看/切换模型
- /cost   — 显示 Token 用量
- /context — 显示上下文占用
- /tasks  — 查看任务列表
- /tools  — 查看可用工具
- /memory — 查看记忆
- /status — 系统状态
- /mode   — 切换交互模式 (normal/auto/plan)
- /think  — 切换思考过程显示
- /restore — 从检查点恢复管道状态
- /quit   — 退出（别名 /exit）

每个命令处理器返回 CommandResult，由 CLI 主循环决定后续行为。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from utils.enum_utils import safe_enum_value

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Slash command execution result.

    Attributes:
        output: Command output text (for console.print)
        should_stop: Whether to terminate the REPL loop
        should_clear_history: Whether to clear conversation history in memory
        should_clear_session: Whether to clear persisted session checkpoints on disk
        state_updates: Extra fields to inject into pipeline state
    """

    output: str | None = None
    should_stop: bool = False
    should_clear_history: bool = False
    should_clear_session: bool = False
    state_updates: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 命令处理器类型
# ---------------------------------------------------------------------------

CommandHandler = Callable[..., Awaitable[CommandResult]]


# ---------------------------------------------------------------------------
# 斜杠命令注册表
# ---------------------------------------------------------------------------


class SlashCommandRegistry:
    """斜杠命令注册表。

    管理所有斜杠命令的注册、查找与执行。

    Example::

        registry = SlashCommandRegistry(console=Console())
        result = await registry.execute("/help", context={})
    """

    def __init__(self, console: Console | None = None) -> None:
        """初始化命令注册表。

        Args:
            console: rich Console 实例，用于输出；默认创建新实例。
        """
        self._console = console or Console()
        self._handlers: dict[str, CommandHandler] = {}
        self._aliases: dict[str, str] = {}
        self._descriptions: dict[str, str] = {}
        self._register_builtin_commands()

    # --- 公共接口 ---

    def register(
        self,
        name: str,
        handler: CommandHandler,
        description: str = "",
        aliases: list[str] | None = None,
    ) -> None:
        """注册斜杠命令。

        Args:
            name: 命令名（不含前缀 /），如 "help"
            handler: 异步命令处理函数
            description: 命令描述
            aliases: 命令别名列表
        """
        self._handlers[name] = handler
        if description:
            self._descriptions[name] = description
        if aliases:
            for alias in aliases:
                self._aliases[alias] = name

    async def execute(
        self,
        raw_input: str,
        context: dict[str, Any] | None = None,
    ) -> CommandResult | None:
        """解析并执行斜杠命令。

        Args:
            raw_input: 用户原始输入（以 / 开头）
            context: 命令执行上下文，包含 engine/services/config 等引用

        Returns:
            命令执行结果；如果不是有效的斜杠命令返回 None
        """
        if not raw_input.startswith("/"):
            return None

        parts = raw_input[1:].strip().split(maxsplit=1)
        if not parts:
            return None

        cmd_name = parts[0].lower()
        cmd_args = parts[1] if len(parts) > 1 else ""

        # 别名解析
        resolved = self._aliases.get(cmd_name, cmd_name)
        handler = self._handlers.get(resolved)

        if handler is None:
            self._console.print(f"[yellow]未知命令: /{cmd_name}[/yellow]  输入 [bold]/help[/bold] 查看可用命令")
            return CommandResult()

        try:
            return await handler(cmd_args, context or {})
        except Exception as exc:
            logger.exception("命令 /%s 执行失败", resolved)
            self._console.print(f"[red]命令执行错误: {exc}[/red]")
            return CommandResult()

    def get_help_text(self) -> str:
        """获取所有命令的帮助文本。"""
        return "输入 /help 查看可用命令"

    def list_commands(self) -> list[tuple[str, str, list[str]]]:
        """列出所有已注册命令。

        Returns:
            列表，每项为 (命令名, 描述, 别名列表)
        """
        result = []
        for name in self._handlers:
            desc = self._descriptions.get(name, "")
            aliases = [a for a, target in self._aliases.items() if target == name]
            result.append((name, desc, aliases))
        return sorted(result, key=lambda x: x[0])

    # --- 内置命令注册 ---

    def _register_builtin_commands(self) -> None:
        """注册内置斜杠命令。"""
        self.register("help", self._cmd_help, "显示可用命令列表", aliases=["h", "?"])
        self.register("compact", self._cmd_compact, "压缩上下文以释放空间")
        self.register("clear", self._cmd_clear, "清空对话历史", aliases=["cls"])
        self.register("model", self._cmd_model, "查看或切换 LLM 模型")
        self.register("cost", self._cmd_cost, "显示 Token 用量统计")
        self.register("context", self._cmd_context, "显示上下文占用情况")
        self.register("tasks", self._cmd_tasks, "查看任务列表")
        self.register("tools", self._cmd_tools, "查看可用工具")
        self.register("memory", self._cmd_memory, "查看记忆摘要")
        self.register("status", self._cmd_status, "显示系统状态")
        self.register("mode", self._cmd_mode, "切换交互模式 (normal/auto/plan)")
        self.register("think", self._cmd_think, "切换思考过程显示 (on/off)")
        self.register("restore", self._cmd_restore, "从检查点恢复管道状态")
        self.register("quit", self._cmd_quit, "退出程序", aliases=["exit", "q"])

    # --- 内置命令实现 ---

    async def _cmd_help(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """显示可用命令列表。"""
        table = Table(
            title="可用命令",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            title_style="bold white",
        )
        table.add_column("命令", style="bold", min_width=12)
        table.add_column("别名", style="dim", min_width=10)
        table.add_column("说明", min_width=20)

        for name, desc, aliases in self.list_commands():
            alias_str = ", ".join(f"/{a}" for a in aliases) if aliases else ""
            table.add_row(f"/{name}", alias_str, desc)

        self._console.print(table)
        self._console.print("\n[dim]快捷语法: @path=文件引用  !cmd=执行命令  #text=追加记忆[/dim]")
        return CommandResult()

    async def _cmd_compact(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """压缩上下文。"""
        services = ctx.get("services", {})
        memory_service = services.get("context_service")

        if memory_service is not None and hasattr(memory_service, "compress_messages"):
            try:
                messages = ctx.get("messages", [])
                context_window = ctx.get("context_window", 128000)
                result = await memory_service.compress_messages(
                    messages=messages,
                    context_window=context_window,
                )
                if result:
                    self._console.print("[green][OK] 上下文已压缩[/green]")
                    return CommandResult(state_updates={"context_compressed": True, "messages": result})
                self._console.print("[yellow][~] 上下文无需压缩[/yellow]")
                return CommandResult()
            except Exception as exc:
                self._console.print(f"[red]压缩失败: {exc}[/red]")
                return CommandResult()

        # 手动压缩：提示管道启用压缩
        self._console.print("[yellow][~] 将在下一轮对话时压缩上下文[/yellow]")
        return CommandResult(state_updates={"compact_requested": True})

    async def _cmd_clear(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """Clear conversation history and persisted session checkpoints."""
        self._console.print("[green][OK] 对话历史已清空，已开启新会话[/green]")
        return CommandResult(should_clear_history=True, should_clear_session=True)

    async def _cmd_model(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """查看或切换模型。"""
        agent_config = ctx.get("agent_config")
        current_model = "unknown"

        if agent_config and hasattr(agent_config, "model"):
            current_model = agent_config.model
        elif agent_config and hasattr(agent_config, "config_id"):
            current_model = agent_config.config_id

        if not args:
            self._console.print(f"[cyan]当前模型:[/cyan] {current_model}")
            self._console.print("[dim]用法: /model <model_name> 切换模型[/dim]")
            return CommandResult()

        # 切换模型
        new_model = args.strip()
        self._console.print(f"[green][OK] 模型已切换: {current_model} -> {new_model}[/green]")
        return CommandResult(state_updates={"model_override": new_model})

    async def _cmd_cost(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """显示 Token 用量统计。"""
        state = ctx.get("last_state", {})

        # 尝试从 state 中获取 token 统计
        total_tokens = state.get("total_tokens", 0)
        prompt_tokens = state.get("prompt_tokens", 0)
        completion_tokens = state.get("completion_tokens", 0)
        total_cost = state.get("total_cost", 0.0)

        # 从 execution_record_storage 获取累计数据
        services = ctx.get("services", {})
        exec_storage = services.get("execution_record_storage")
        if exec_storage is not None and hasattr(exec_storage, "get_stats"):
            try:
                stats = exec_storage.get_stats()
                if stats:
                    total_tokens = stats.get("total_tokens", total_tokens)
                    total_cost = stats.get("total_cost", total_cost)
            except Exception:
                pass

        table = Table(title="Token 用量", border_style="dim", title_style="bold white")
        table.add_column("指标", style="cyan")
        table.add_column("值", style="green", justify="right")
        table.add_row("Prompt Tokens", f"{prompt_tokens:,}")
        table.add_row("Completion Tokens", f"{completion_tokens:,}")
        table.add_row("Total Tokens", f"{total_tokens:,}")
        table.add_row("Estimated Cost", f"${total_cost:.4f}")

        self._console.print(table)
        return CommandResult()

    async def _cmd_context(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """显示上下文占用情况。"""
        conversation_history = ctx.get("conversation_history", [])
        ctx.get("last_state", {})

        # 估算上下文大小
        msg_count = len(conversation_history)
        char_count = sum(
            len(m.get("content", "")) if isinstance(m, dict) else len(str(m)) for m in conversation_history
        )

        # 粗略估算 token 数（中文约 1.5 字/token，英文约 4 字符/token）
        estimated_tokens = char_count // 3
        max_context = 128000  # 默认上下文窗口
        usage_pct = min(100, estimated_tokens / max_context * 100) if max_context else 0

        # 构建进度条
        bar_len = 30
        filled = int(bar_len * usage_pct / 100)
        bar = "=" * filled + "-" * (bar_len - filled)

        color = "green" if usage_pct < 50 else ("yellow" if usage_pct < 80 else "red")

        self._console.print(f"[cyan]上下文占用:[/cyan] [{color}]{bar}[/{color}] {usage_pct:.1f}%")
        self._console.print(
            f"  消息数: {msg_count}  |  字符数: {char_count:,}  |  "
            f"估算 Token: {estimated_tokens:,}  |  窗口: {max_context:,}"
        )
        return CommandResult()

    async def _cmd_tasks(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """查看任务列表。"""
        services = ctx.get("services", {})
        task_service = services.get("task_service")

        if task_service is None:
            self._console.print("[yellow]任务服务未启用[/yellow]")
            return CommandResult()

        try:
            # 尝试获取任务列表
            if hasattr(task_service, "list_tasks"):
                tasks = task_service.list_tasks()
            elif hasattr(task_service, "get_all_tasks"):
                tasks = task_service.get_all_tasks()
            else:
                self._console.print("[yellow]任务服务接口不支持列表查询[/yellow]")
                return CommandResult()

            if not tasks:
                self._console.print("[dim]暂无任务[/dim]")
                return CommandResult()

            table = Table(title="任务列表", border_style="dim", title_style="bold white")
            table.add_column("ID", style="dim", min_width=8)
            table.add_column("状态", min_width=10)
            table.add_column("描述", min_width=20)

            for task in tasks[:20]:  # 最多显示 20 条
                if isinstance(task, dict):
                    tid = str(task.get("task_id", task.get("id", "?")))
                    status = str(task.get("status", "?"))
                    desc = str(task.get("description", task.get("title", "")))
                else:
                    tid = str(getattr(task, "task_id", getattr(task, "id", "?")))
                    status = str(getattr(task, "status", "?"))
                    desc = str(getattr(task, "description", getattr(task, "title", "")))

                # 状态着色
                status_color = {
                    "completed": "green",
                    "done": "green",
                    "running": "cyan",
                    "in_progress": "cyan",
                    "pending": "yellow",
                    "failed": "red",
                    "error": "red",
                }.get(status.lower(), "white")

                table.add_row(tid, f"[{status_color}]{status}[/{status_color}]", desc[:60])

            self._console.print(table)
            if len(tasks) > 20:
                self._console.print(f"[dim]... 还有 {len(tasks) - 20} 个任务[/dim]")

        except Exception as exc:
            self._console.print(f"[red]获取任务列表失败: {exc}[/red]")

        return CommandResult()

    async def _cmd_tools(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """查看可用工具。"""
        services = ctx.get("services", {})
        tool_registry = services.get("tool_registry")

        if tool_registry is None:
            self._console.print("[yellow]工具注册表未启用[/yellow]")
            return CommandResult()

        try:
            tools = tool_registry.list_tools()
        except Exception:
            tools = []

        if not tools:
            self._console.print("[dim]暂无注册工具[/dim]")
            return CommandResult()

        table = Table(title="可用工具", border_style="dim", title_style="bold white")
        table.add_column("名称", style="bold cyan", min_width=18)
        table.add_column("说明", min_width=30)

        for tool in tools:
            name = tool.get("name", "?") if isinstance(tool, dict) else str(tool)
            desc = tool.get("description", "") if isinstance(tool, dict) else ""
            table.add_row(name, desc[:80])

        self._console.print(table)
        return CommandResult()

    async def _cmd_memory(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """查看记忆摘要。"""
        services = ctx.get("services", {})
        memory_service = services.get("memory_service")

        if memory_service is None:
            self._console.print("[yellow]记忆服务未启用[/yellow]")
            return CommandResult()

        try:
            # 尝试获取最近记忆
            if hasattr(memory_service, "get_recent"):
                episodes = memory_service.get_recent(limit=10)
            elif hasattr(memory_service, "search"):
                episodes = memory_service.search("", limit=10)
            else:
                self._console.print("[dim]记忆服务接口不支持查询[/dim]")
                return CommandResult()

            if not episodes:
                self._console.print("[dim]暂无记忆[/dim]")
                return CommandResult()

            table = Table(title="最近记忆", border_style="dim", title_style="bold white")
            table.add_column("#", style="dim", width=4)
            table.add_column("内容", min_width=40)
            table.add_column("时间", style="dim", min_width=16)

            for i, ep in enumerate(episodes[:10], 1):
                if isinstance(ep, dict):
                    content = str(ep.get("content", ep.get("text", "")))[:60]
                    ts = str(ep.get("timestamp", ep.get("created_at", "")))[:16]
                else:
                    content = str(getattr(ep, "content", getattr(ep, "text", "")))[:60]
                    ts = str(getattr(ep, "timestamp", getattr(ep, "created_at", "")))[:16]
                table.add_row(str(i), content, ts)

            self._console.print(table)

        except Exception as exc:
            self._console.print(f"[red]获取记忆失败: {exc}[/red]")

        return CommandResult()

    async def _cmd_status(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """显示系统状态。"""
        agent_config = ctx.get("agent_config")
        services = ctx.get("services", {})
        conversation_history = ctx.get("conversation_history", [])
        mode = ctx.get("mode", "normal")
        turn_count = ctx.get("turn_count", 0)

        # Agent 信息
        agent_name = "Agent OS"
        agent_level = "N/A"
        if agent_config:
            agent_name = getattr(agent_config, "display_name", agent_name)
            level = getattr(agent_config, "level", None)
            if level:
                agent_level = safe_enum_value(level)

        # 服务状态
        svc_status: list[tuple[str, str]] = []
        for name in ("tool_registry", "memory_store", "memory_service", "task_service"):
            svc = services.get(name)
            status = "[green]OK[/green]" if svc is not None else "[dim]--[/dim]"
            svc_status.append((name, status))

        # 工具数量
        tool_count = 0
        tool_registry = services.get("tool_registry")
        if tool_registry is not None:
            with contextlib.suppress(Exception):
                tool_count = len(tool_registry.list_tools())

        # 输出
        self._console.print(
            Panel(
                f"[bold]Agent:[/bold] {agent_name} (Level: {agent_level})\n"
                f"[bold]模式:[/bold] {mode}  |  [bold]轮次:[/bold] {turn_count}  |  "
                f"[bold]历史消息:[/bold] {len(conversation_history)}\n"
                f"[bold]工具数:[/bold] {tool_count}\n"
                "\n[bold]服务状态:[/bold]",
                title="系统状态",
                border_style="cyan",
            )
        )

        for svc_name, svc_stat in svc_status:
            self._console.print(f"  {svc_stat} {svc_name}")

        return CommandResult()

    async def _cmd_mode(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """切换交互模式。"""
        valid_modes = {"normal", "auto", "plan"}
        current_mode = ctx.get("mode", "normal")

        if not args:
            self._console.print(f"[cyan]当前模式:[/cyan] {current_mode}")
            self._console.print("[dim]用法: /mode <normal|auto|plan>[/dim]")
            self._console.print(
                "  [bold]normal[/bold] — 正常对话，工具调用需确认\n"
                "  [bold]auto[/bold]   — 自动执行，不需确认\n"
                "  [bold]plan[/bold]   — 只读模式，只规划不执行"
            )
            return CommandResult()

        new_mode = args.strip().lower()
        if new_mode not in valid_modes:
            self._console.print(f"[red]无效模式: {new_mode}[/red]  可选: {', '.join(valid_modes)}")
            return CommandResult()

        mode_desc = {
            "normal": "正常对话，工具调用需确认",
            "auto": "自动执行，不需确认",
            "plan": "只读模式，只规划不执行",
        }

        self._console.print(
            f"[green][OK] 模式已切换: {current_mode} -> {new_mode}[/green]  [dim]({mode_desc[new_mode]})[/dim]"
        )
        return CommandResult(state_updates={"interaction_mode": new_mode})

    async def _cmd_think(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """切换思考过程显示。"""
        current = ctx.get("show_thinking", False)

        if not args:
            state_str = "开启" if current else "关闭"
            self._console.print(f"[cyan]思考过程显示:[/cyan] {state_str}")
            self._console.print("[dim]用法: /think <on|off>[/dim]")
            return CommandResult()

        val = args.strip().lower()
        if val in ("on", "true", "1", "yes", "开"):
            new_val = True
        elif val in ("off", "false", "0", "no", "关"):
            new_val = False
        else:
            self._console.print(f"[red]无效参数: {val}[/red]  可选: on/off")
            return CommandResult()

        state_str = "开启" if new_val else "关闭"
        self._console.print(f"[green][OK] 思考过程显示: {state_str}[/green]")
        return CommandResult(state_updates={"show_thinking": new_val})

    async def _cmd_restore(self, args: str, ctx: dict[str, Any]) -> CommandResult:  # noqa: PLR0911
        """从检查点恢复管道状态。"""
        services = ctx.get("services", {})
        pipeline_recovery = services.get("pipeline_recovery")

        if pipeline_recovery is None:
            self._console.print("[yellow]管道恢复服务未启用[/yellow]")
            return CommandResult()

        checkpoint_manager = getattr(pipeline_recovery, "checkpoint_manager", None)
        if checkpoint_manager is None:
            self._console.print("[yellow]检查点管理器不可用[/yellow]")
            return CommandResult()

        try:
            # 列出可用检查点
            checkpoints = await checkpoint_manager.list_checkpoints(limit=20)

            if not checkpoints:
                self._console.print("[dim]暂无可用检查点[/dim]")
                return CommandResult()

            # 显示检查点列表
            table = Table(title="可用检查点", border_style="dim", title_style="bold white")
            table.add_column("#", style="dim", width=4)
            table.add_column("检查点 ID", style="cyan", min_width=30)
            table.add_column("管道", style="dim", min_width=12)
            table.add_column("阶段", min_width=12)
            table.add_column("轮次", justify="right", min_width=6)
            table.add_column("时间", style="dim", min_width=20)

            for i, cp in enumerate(checkpoints, 1):
                cp_id = str(cp.get("checkpoint_id", "?"))
                pipeline_id = str(cp.get("pipeline_id", ""))
                phase = str(cp.get("phase", ""))
                iteration = str(cp.get("iteration", 0))
                timestamp = str(cp.get("timestamp", ""))[:19]
                table.add_row(str(i), cp_id, pipeline_id, phase, iteration, timestamp)

            self._console.print(table)

            # 如果带参数，直接按索引恢复
            if args.strip():
                try:
                    idx = int(args.strip())
                    if 1 <= idx <= len(checkpoints):
                        selected = checkpoints[idx - 1]
                        return await self._do_restore(
                            pipeline_recovery,
                            str(selected.get("pipeline_id", "")),
                            ctx,
                        )
                    self._console.print(f"[red]无效索引: {idx}，范围 1-{len(checkpoints)}[/red]")
                    return CommandResult()
                except ValueError:
                    # 参数当作 pipeline_id 处理
                    return await self._do_restore(pipeline_recovery, args.strip(), ctx)

            # 无参数时提示用法
            self._console.print("\n[dim]用法: /restore <序号>  或  /restore <pipeline_id>[/dim]")

        except Exception as exc:
            self._console.print(f"[red]获取检查点列表失败: {exc}[/red]")

        return CommandResult()

    async def _do_restore(
        self,
        pipeline_recovery: Any,
        pipeline_id: str,
        ctx: dict[str, Any],
    ) -> CommandResult:
        """执行管道恢复。

        Args:
            pipeline_recovery: PipelineRecovery 实例
            pipeline_id: 管道 ID
            ctx: 命令执行上下文

        Returns:
            命令执行结果
        """
        try:
            # 获取恢复信息
            info = await pipeline_recovery.get_recovery_info(pipeline_id)
            if info is None:
                self._console.print(f"[yellow]未找到管道 '{pipeline_id}' 的检查点[/yellow]")
                return CommandResult()

            checkpoint_meta = info.get("checkpoint", {})
            suggestion = info.get("recovery_suggestion", "")

            self._console.print(
                Panel(
                    f"[bold]管道:[/bold] {pipeline_id}\n"
                    f"[bold]检查点:[/bold] {checkpoint_meta.get('checkpoint_id', '?')}\n"
                    f"[bold]阶段:[/bold] {checkpoint_meta.get('phase', '?')}\n"
                    f"[bold]轮次:[/bold] {checkpoint_meta.get('iteration', 0)}\n"
                    f"[bold]时间:[/bold] {checkpoint_meta.get('timestamp', '?')}\n"
                    f"\n[dim]建议: {suggestion}[/dim]",
                    title="恢复信息",
                    border_style="cyan",
                )
            )

            # 恢复状态
            state = await pipeline_recovery.recover(pipeline_id)
            if state is None:
                self._console.print("[red]恢复失败：无法加载检查点状态[/red]")
                return CommandResult()

            self._console.print(f"[green][OK] 管道状态已恢复 | state_keys={list(state.keys())[:5]}...[/green]")
            return CommandResult(state_updates={"restored_state": state})

        except Exception as exc:
            self._console.print(f"[red]恢复失败: {exc}[/red]")
            return CommandResult()

    async def _cmd_quit(self, args: str, ctx: dict[str, Any]) -> CommandResult:
        """退出程序。"""
        self._console.print("[bold blue]感谢使用 Agent OS，再见！[/bold blue]")
        return CommandResult(should_stop=True)


# ---------------------------------------------------------------------------
# 行内快捷语法解析
# ---------------------------------------------------------------------------


def parse_inline_shortcuts(text: str) -> tuple[str, dict[str, Any]]:
    """解析行内快捷语法。

    支持的快捷语法：
    - @path — 文件引用（标记 file_refs）
    - !command — 直接执行 bash（标记 bash_cmd）
    - #text — 追加记忆（标记 memory_note）

    Args:
        text: 用户原始输入文本

    Returns:
        (处理后的文本, 附加状态字典)
    """
    import re  # noqa: PLC0415

    extras: dict[str, Any] = {
        "file_refs": [],
        "bash_cmds": [],
        "memory_notes": [],
    }

    # @path 文件引用 — 识别 @xxx 模式
    file_refs = re.findall(r"@([\w./\\-]+\.[\w]+)", text)
    if file_refs:
        extras["file_refs"] = file_refs

    # !command bash 命令
    bash_cmds = re.findall(r"!(\w+.*)", text)
    if bash_cmds:
        extras["bash_cmds"] = bash_cmds

    # #text 追加记忆
    memory_notes = re.findall(r"#([\u4e00-\u9fff\w][\u4e00-\u9fff\w\s]{0,50})", text)
    # 过滤掉看起来像标题的（以 # 开头的 markdown 标题）
    memory_notes = [n for n in memory_notes if not n.startswith("#")]
    if memory_notes:
        extras["memory_notes"] = memory_notes

    # 清除 extras 中空列表
    extras = {k: v for k, v in extras.items() if v}

    return text, extras
