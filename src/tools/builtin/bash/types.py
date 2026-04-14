"""
Bash 工具类型定义

暴露接口：
- BashAction：BashAction类
- OutputType：OutputType类
- OutputSummary：OutputSummary类
- ProcessInfo：ProcessInfo类
- LogCompressorConfig：LogCompressorConfig类
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class BashAction(str, Enum):
    """Bash 工具操作类型"""
    EXECUTE = "execute"      # 执行新命令
    CONTINUE = "continue"    # 继续运行中的命令
    TERMINATE = "terminate"  # 终止运行中的命令
    INPUT = "input"          # 向运行中的进程发送输入
    READ_LOG = "read_log"    # 读取命令日志


class OutputType(str, Enum):
    """输出类型检测"""
    NPM_INSTALL = "npm_install"
    PIP_INSTALL = "pip_install"
    DOCKER_BUILD = "docker_build"
    PYTEST = "pytest"
    COMPILATION = "compilation"
    GIT = "git"
    GENERAL = "general"


class LogCompressorConfig(BaseModel):
    """
    日志压缩配置

    用于控制日志压缩器的行为参数，包括压缩阈值、显示行数、错误处理等。

    Attributes:
        compress_threshold: 压缩阈值（行数），当日志行数超过此值时触发压缩
        recent_lines: 最近行数，压缩时保留的最近日志行数
        show_errors: 是否显示错误列表
        dedup_errors: 是否合并重复错误
    """

    compress_threshold: int = 1000  # 压缩阈值（行数）
    recent_lines: int = 10           # 最近行数
    show_errors: bool = True          # 是否显示错误列表
    dedup_errors: bool = True        # 是否合并重复错误


@dataclass
class OutputSummary:
    """输出摘要"""
    lines: list[str] = field(default_factory=list)
    output_type: OutputType = OutputType.GENERAL
    total_lines: int = 0
    warnings: int = 0
    errors: int = 0
    progress: str | None = None
    latest_message: str = ""


@dataclass
class ProcessInfo:
    """进程信息"""
    pid: int
    command: str
    start_time: float
    log_file: Path
    process: asyncio.subprocess.Process | None = None
    status: str = "running"  # running, completed, terminated, error
    exit_code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output_task: asyncio.Task | None = None  # 输出读取任务引用，防止垃圾回收
