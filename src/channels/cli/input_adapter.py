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
import uuid
from typing import Any

from channels.cli.cli_commands import CommandResult, SlashCommandRegistry
from channels.input_adapter import IInputAdapter

logger = logging.getLogger(__name__)


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

    @property
    def command_registry(self) -> SlashCommandRegistry:
        """获取斜杠命令注册表。"""
        return self._command_registry

    @property
    def last_command_result(self) -> CommandResult | None:
        """获取最近一次斜杠命令的执行结果。"""
        return self._last_command_result

    async def receive(self) -> dict[str, Any]:
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
            return {
                "user_input": "",
                "core_type": "llm_call",
                "session_id": str(uuid.uuid4()),
                "should_stop": True,
                "iteration": 1,
            }

        stripped = user_input.strip()

        # 空输入 -- 不停止，返回空消息
        if not stripped:
            return {
                "user_input": "",
                "core_type": "llm_call",
                "session_id": str(uuid.uuid4()),
                "should_stop": False,
                "iteration": 1,
                "_is_empty": True,
            }

        # 退出命令
        if stripped.lower() in ("quit", "exit", "q"):
            return {
                "user_input": stripped,
                "core_type": "llm_call",
                "session_id": str(uuid.uuid4()),
                "should_stop": True,
                "iteration": 1,
            }

        # 斜杠命令 -- 标记但不在这里执行（由 CLIApplication 处理）
        if stripped.startswith("/"):
            return {
                "user_input": stripped,
                "core_type": "llm_call",
                "session_id": str(uuid.uuid4()),
                "should_stop": False,
                "iteration": 1,
                "_is_slash_command": True,
            }

        # 普通输入 -- 解析行内快捷语法
        from channels.cli.cli_commands import parse_inline_shortcuts
        processed_text, inline_extras = parse_inline_shortcuts(stripped)

        state: dict[str, Any] = {
            "user_input": processed_text,
            "core_type": "llm_call",
            "session_id": str(uuid.uuid4()),
            "should_stop": False,
            "iteration": 1,
        }

        if inline_extras:
            state["_inline_extras"] = inline_extras

        return state

    def _read_multiline(self) -> str:
        """读取多行输入。

        支持 \\ 续行：行尾的反斜杠表示输入未结束，
        继续读取下一行直到遇到无反斜杠结尾的行为止。

        Returns:
            拼接后的完整输入文本
        """
        lines: list[str] = []

        # 首行
        line = input(f"\n{self._prompt_str}")
        lines.append(line)

        # 续行检测：行尾有 \\ 表示续接
        while lines[-1].rstrip().endswith("\\"):
            # 去掉末尾的续行符
            lines[-1] = lines[-1].rstrip()[:-1]
            try:
                continuation = input(self._continuation_prompt)
                lines.append(continuation)
            except (EOFError, KeyboardInterrupt):
                break

        return "\n".join(lines)

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
