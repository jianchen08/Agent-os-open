"""
增强版 Bash 命令执行工具

提供高级功能：
- 支持长时间运行的进程（30秒阈值 + 回调机制）
- 支持交互式输入（确认、密码等）
- 智能日志压缩（3-5行摘要）
- 与隔离系统集成（Host/Container/Sandbox）

注意：此文件为兼容层，实际实现已迁移到 src/tools/builtin/bash/ 目录
"""

# 从新的包结构中重新导出所有内容，保持向后兼容
from src.tools.builtin.bash import (
    BashAction,
    BashTool,
    InputHandler,
    LogCompressor,
    OutputSummary,
    OutputType,
    ProcessInfo,
    ProcessManager,
    SecurityChecker,
)

__all__ = [
    "BashTool",
    "BashAction",
    "OutputType",
    "OutputSummary",
    "ProcessInfo",
    "LogCompressor",
    "InputHandler",
    "ProcessManager",
    "SecurityChecker",
]
