"""CLI 输入适配器模块（Claude Code 风格）。

从标准输入读取用户命令行输入，支持：
- 斜杠命令解析（/help, /clear, /mode 等）
- 多行输入（\\ 换行续接）
- 空行提交
- 行内快捷语法（@path, !cmd, #text）
- 退出命令检测

转换为管道引擎的初始 state。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import uuid
from typing import Any

from channels.cli.cli_commands import CommandResult, SlashCommandRegistry
from channels.input_adapter import IInputAdapter

logger = logging.getLogger(__name__)


class _StdinLineReader:
    """后台线程持续从 stdin 逐行读取，放入队列。

    使所有平台（含 Windows）都能进行带超时的非阻塞 stdin 读取，
    从而正确检测多行粘贴事件。
    """

    _INTERRUPT = object()

    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._eof = False
        self._interrupt_event = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._started = False
        self._was_interrupted = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def _read_loop(self) -> None:
        while True:
            try:
                line = sys.stdin.readline()
                if not line:  # EOF
                    self._eof = True
                    self._queue.put(None)
                    break
                if line.endswith("\n"):
                    line = line[:-1]
                if line.endswith("\r"):
                    line = line[:-1]
                self._queue.put(line)
            except Exception:
                self._eof = True
                self._queue.put(None)
                break

    def read_line_blocking(self) -> str | None:
        """阻塞读取一行。返回 None 表示 EOF。

        通过轮询队列（0.5s 超时）实现可中断读取。
        """
        if self._eof and self._queue.empty():
            return None
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._interrupt_event.is_set():
                    self._interrupt_event.clear()
                    self._was_interrupted = True
                    return None
                continue
            if item is self._INTERRUPT:
                self._was_interrupted = True
                return None
            return item

    def drain(self) -> list[str]:
        """清空队列中所有未消费的行。"""
        self._was_interrupted = False
        lines: list[str] = []
        while True:
            try:
                item = self._queue.get_nowait()
                if item is not None and item is not self._INTERRUPT:
                    lines.append(item)
            except queue.Empty:
                break
        self._interrupt_event.clear()
        return lines

    def interrupt(self) -> None:
        """中断当前阻塞的读取操作。"""
        self._queue.put(self._INTERRUPT)
        self._interrupt_event.set()

    @property
    def was_interrupted(self) -> bool:
        """上一次 read_line_blocking 返回 None 是因为 interrupt 而非真实 EOF。"""
        return self._was_interrupted

    def clear_interrupt_flag(self) -> None:
        """清除 interrupt 标记。"""
        self._was_interrupted = False

    def read_line(self, timeout: float) -> str | None:
        """带超时读取一行。超时或 EOF 返回 None。"""
        if self._eof and self._queue.empty():
            return None
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class CLIInputAdapter(IInputAdapter):
    """命令行输入适配器（Claude Code 风格）。

    从标准输入读取用户输入，支持斜杠命令、多行输入、
    行内快捷语法，将其封装为管道引擎可处理的初始 state。

    Args:
        prompt_str: 输入提示符字符串，默认为 "> "。
        command_registry: 斜杠命令注册表实例。

    Example::

        from channels.cli.cli_commands import SlashCommandRegistry
        registry = SlashCommandRegistry()
        adapter = CLIInputAdapter(prompt_str="> ", command_registry=registry)
        state = await adapter.receive()
    """

    def __init__(
        self,
        prompt_str: str = "> ",
        command_registry: SlashCommandRegistry | None = None,
    ) -> None:
        """初始化 CLI 输入适配器。

        Args:
            prompt_str: 输入提示符，显示在用户输入之前。
            command_registry: 斜杠命令注册表；默认创建新实例。
        """
        self._prompt_str = prompt_str
        self._continuation_prompt = "... "  # 多行续接提示符
        self._command_registry = command_registry or SlashCommandRegistry()
        self._last_command_result: CommandResult | None = None
        self._stdin_reader: _StdinLineReader | None = None
        self._paste_line_count: int = 0

    def _get_stdin_reader(self) -> _StdinLineReader:
        """延迟初始化后台 stdin 读取线程。"""
        if self._stdin_reader is None:
            self._stdin_reader = _StdinLineReader()
            self._stdin_reader.start()
        return self._stdin_reader

    def drain_stdin(self) -> list[str]:
        """清空 stdin 缓冲区中的未消费输入。"""
        if self._stdin_reader is not None:
            return self._stdin_reader.drain()
        return []

    def interrupt_stdin(self) -> None:
        """中断当前阻塞的 stdin 读取。"""
        if self._stdin_reader is not None:
            self._stdin_reader.interrupt()

    @property
    def command_registry(self) -> SlashCommandRegistry:
        """获取斜杠命令注册表。"""
        return self._command_registry

    @property
    def last_command_result(self) -> CommandResult | None:
        """获取最近一次斜杠命令的执行结果。"""
        return self._last_command_result

    def prompt_text(self) -> str:
        """返回当前提示符文本（含前导换行）。"""
        return f"\n{self._prompt_str}"

    async def receive(self) -> dict[str, Any]:  # noqa: PLR0911
        """从 stdin 读取用户输入，返回初始 state。

        支持：
        - 斜杠命令：以 / 开头的输入被标记为 _is_slash_command
        - 多行输入：行尾 \\ 表示续接下一行
        - 空行提交：空输入返回 should_stop=False（允许空消息）
        - 退出命令：quit/exit/q 设置 should_stop=True

        Returns:
            初始管道状态字典，包含：
                - user_input: 用户输入的文本
                - core_type: 核心处理类型，固定为 "llm_call"
                - session_id: 唯一会话标识（UUID4）
                - should_stop: 是否应停止循环
                - iteration: 迭代计数，初始为 1
                - _is_slash_command: 是否为斜杠命令
                - _is_empty: 是否为空输入
                - _inline_extras: 行内快捷语法解析结果
        """
        try:
            loop = asyncio.get_running_loop()
            user_input = await loop.run_in_executor(None, self._read_multiline)
        except (EOFError, KeyboardInterrupt):
            reader = self._stdin_reader
            if reader and reader.was_interrupted:
                reader.clear_interrupt_flag()
                return {
                    "user_input": "",
                    "core_type": "llm_call",
                    "session_id": uuid.uuid4().hex[:12],
                    "should_stop": False,
                    "iteration": 1,
                    "_is_empty": True,
                    "_interrupted": True,
                }
            return {
                "user_input": "",
                "core_type": "llm_call",
                "session_id": uuid.uuid4().hex[:12],
                "should_stop": True,
                "iteration": 1,
            }
        except Exception as _read_exc:
            import logging as _logging  # noqa: PLC0415

            _logging.getLogger(__name__).warning(
                "[InputAdapter] receive() unexpected error: %s",
                _read_exc,
                exc_info=True,
            )
            return {
                "user_input": "",
                "core_type": "llm_call",
                "session_id": uuid.uuid4().hex[:12],
                "should_stop": False,
                "iteration": 1,
                "_is_empty": True,
            }

        stripped = user_input.strip()

        # 空输入 -- 不停止，返回空消息
        if not stripped:
            return {
                "user_input": "",
                "core_type": "llm_call",
                "session_id": uuid.uuid4().hex[:12],
                "should_stop": False,
                "iteration": 1,
                "_is_empty": True,
            }

        # 退出命令
        if stripped.lower() in ("quit", "exit", "q"):
            return {
                "user_input": stripped,
                "core_type": "llm_call",
                "session_id": uuid.uuid4().hex[:12],
                "should_stop": True,
                "iteration": 1,
            }

        # 斜杠命令 -- 标记但不在这里执行（由 CLIApplication 处理）
        if stripped.startswith("/"):
            return {
                "user_input": stripped,
                "core_type": "llm_call",
                "session_id": uuid.uuid4().hex[:12],
                "should_stop": False,
                "iteration": 1,
                "_is_slash_command": True,
            }

        # 普通输入 -- 解析行内快捷语法
        from channels.cli.cli_commands import parse_inline_shortcuts  # noqa: PLC0415

        processed_text, inline_extras = parse_inline_shortcuts(stripped)

        state: dict[str, Any] = {
            "user_input": processed_text,
            "core_type": "llm_call",
            "session_id": uuid.uuid4().hex[:12],
            "should_stop": False,
            "iteration": 1,
        }

        if inline_extras:
            state["_inline_extras"] = inline_extras

        return state

    def _read_line(self, prompt: str) -> str:
        """从队列读取一行输入（不显示提示符）。

        提示符由调用方在显示后再调用此方法。

        Args:
            prompt: 未使用，保留接口兼容

        Returns:
            用户输入的一行文本（不含换行符）
        """
        reader = self._get_stdin_reader()
        line = reader.read_line_blocking()
        if line is None:
            raise EOFError
        return line

    def _read_multiline(self) -> str:
        """读取多行输入。

        支持：
        - 反斜杠续行：行尾的 \\ 表示输入未结束
        - 多行粘贴：快速连续到达的行自动合并为一条消息

        Returns:
            拼接后的完整输入文本
        """
        self._paste_line_count = 0
        lines: list[str] = []

        # 首行（提示符已由主循环显示）
        line = self._read_line("")
        lines.append(line)

        # 多行粘贴检测：快速到达的额外行合并为同一条消息
        self._drain_paste_lines(lines)

        # 续行检测：行尾有 \\ 表示续接
        while lines[-1].rstrip().endswith("\\"):
            # 去掉末尾的续行符
            lines[-1] = lines[-1].rstrip()[:-1]
            try:
                continuation = self._read_line(self._continuation_prompt)
                lines.append(continuation)
                # 续行后也可能有粘贴
                self._drain_paste_lines(lines)
            except (EOFError, KeyboardInterrupt):
                break

        return "\n".join(lines)

    def _drain_paste_lines(self, lines: list[str]) -> None:
        """读取快速连续到达的额外行（多行粘贴检测）。

        使用后台 stdin 读取线程 + 队列，支持所有平台（含 Windows）。
        粘贴的多行会立即出现在队列中，可通过短超时一次性收集；
        手动输入因行间延迟较大，不会误合并。
        """
        is_tty = sys.stdin.isatty()

        if not is_tty:
            return

        reader = self._get_stdin_reader()

        # 读取粘贴的额外行：粘贴数据在队列中立即可用
        extra = 0
        while True:
            line = reader.read_line(timeout=0.05)
            if line is None:
                break
            lines.append(line)
            extra += 1

        if extra > 0:
            self._paste_line_count = extra
            logger.info("粘贴检测: 合并 %d 行额外输入", extra)

    def is_slash_command(self, state: dict[str, Any]) -> bool:
        """判断 state 是否来自斜杠命令输入。

        Args:
            state: 管道状态字典

        Returns:
            是否为斜杠命令
        """
        return state.get("_is_slash_command", False)

    def is_empty_input(self, state: dict[str, Any]) -> bool:
        """判断 state 是否为空输入。

        Args:
            state: 管道状态字典

        Returns:
            是否为空输入
        """
        return state.get("_is_empty", False)

    def was_paste(self) -> bool:
        """最近一次输入是否为多行粘贴。

        Returns:
            是否检测到粘贴
        """
        return self._paste_line_count > 0

    def paste_line_count(self) -> int:
        """最近一次粘贴的额外行数。

        Returns:
            额外行数（不含首行）
        """
        return self._paste_line_count

    async def receive_with_timeout(self, timeout: int = 60) -> dict[str, Any] | None:
        """带超时的异步输入。超时返回 None。

        将同步阻塞的 receive() 方法包装为异步执行，
        在指定超时时间内未完成则返回 None。

        Args:
            timeout: 超时秒数，默认 60

        Returns:
            管道状态字典，超时则返回 None
        """
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self.receive),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return None
