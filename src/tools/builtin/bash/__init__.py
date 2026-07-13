"""
增强版 Bash 命令执行工具

提供：
- 支持长时间运行的进程（30秒阈值 + 回调机制）
- 支持交互式输入（确认、密码等）
- 智能日志压缩（3-5行摘要）
- 与隔离系统集成（Host/Container）
- 自适应编码处理（UTF-8 / 系统编码 GBK 自动识别）
"""

from .encoding import EncodingHandler
from .input_handler import InputHandler
from .log_compressor import LogCompressor
from .process_manager import ProcessManager
from .tool import BashTool, SecurityChecker
from .types import BashAction, OutputSummary, OutputType, ProcessInfo

__all__ = [
    "BashTool",
    "BashAction",
    "EncodingHandler",
    "InputHandler",
    "LogCompressor",
    "OutputSummary",
    "OutputType",
    "ProcessInfo",
    "ProcessManager",
    "SecurityChecker",
]
