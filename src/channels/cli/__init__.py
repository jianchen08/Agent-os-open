"""CLI 通道模块（Claude Code 风格）。

提供命令行界面的输入/输出适配器和斜杠命令系统：
- CLIInputAdapter: 从 stdin 读取用户输入，支持斜杠命令和多行输入
- CLIOutputAdapter: 使用 rich 库进行增强的彩色输出
- SlashCommandRegistry: 斜杠命令注册表与处理器
- CLIApplication: Claude Code 风格 CLI 应用主类（需从 cli_main 单独导入）
"""

from channels.cli.cli_commands import CommandResult, SlashCommandRegistry
from channels.cli.input_adapter import CLIInputAdapter
from channels.cli.output_adapter import CLIOutputAdapter, StatusBarRenderer

__all__ = [
    "CLIInputAdapter",
    "CLIOutputAdapter",
    "CommandResult",
    "SlashCommandRegistry",
    "StatusBarRenderer",
]
