"""
Bash 工具类型定义

暴露接口：
- BashAction：BashAction类
- OutputType：OutputType类
- OutputSummary：OutputSummary类
- ProcessInfo：ProcessInfo类
- ProcessBackend：进程执行/清理后端抽象（工具层）
- WorkUnit：一次命令执行的可追踪、可杀句柄
- LogCompressorConfig：LogCompressorConfig类
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class BashAction(str, Enum):
    """Bash 工具操作类型"""

    EXECUTE = "execute"  # 执行新命令
    CONTINUE = "continue"  # 继续运行中的命令
    TERMINATE = "terminate"  # 终止运行中的命令
    INPUT = "input"  # 向运行中的进程发送输入
    READ_LOG = "read_log"  # 读取命令日志


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
    recent_lines: int = 10  # 最近行数
    show_errors: bool = True  # 是否显示错误列表
    dedup_errors: bool = True  # 是否合并重复错误


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
    # 最近一次被外部访问的时间（任何 get/send_input/terminate 调用都更新）。
    # 看门狗据此判定进程是否已被 Agent 遗弃：内存紧张时按 idle(=now-last_access)
    # 排序，杀最久没访问的；idle>30min 无条件兜底杀。活跃进程(agent 一直访问)
    # idle≈0，永不被杀。
    last_access_time: float = 0.0
    # 该工作单元所属的进程后端（看门狗杀进程时调 backend.kill）。
    # 本地 bash 用 LocalProcessBackend，容器隔离用 ContainerProcessBackend。
    backend: ProcessBackend | None = None


@dataclass
class WorkUnit:
    """一次命令执行的可追踪、可杀句柄。

    进程后端 launch 时返回。看门狗通过它定位并杀掉对应工作单元的整棵进程树。
    - 本地后端：pid 是真实 OS pid，psutil 据此递归杀树。
    - 容器后端：pgid 是容器内进程组号，docker exec kill -- -pgid 整组杀。
    """

    pid: int  # 主进程 pid（本地=OS pid；容器内可作标识）
    command: str
    pgid: int | None = None  # 进程组号（容器后端用，本地后端可不填）
    metadata: dict[str, Any] = field(default_factory=dict)


class ProcessBackend(ABC):
    """进程执行/清理后端抽象（工具层）。

    杀进程是工具的能力，隔离模式只是接入这个抽象的一种后端实现。
    - LocalProcessBackend：本地宿主执行，psutil 递归杀进程树。
    - ContainerProcessBackend：容器内 docker exec 执行，进程组整组杀。

    看门狗（ProcessManager）的策略层只依赖此抽象，不关心进程跑在哪。
    """

    @abstractmethod
    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        """杀掉该工作单元的整棵进程树。

        必须杀整树（含所有后代），否则孙子进程(cargo/rustc/cc)变孤儿继续跑，
        是 setns 故障和 PID 耗尽的根因。
        """

    @abstractmethod
    async def sample_memory(self) -> float | None:
        """采样当前后端的内存使用率（0~1）。

        返回 None 表示采样不可用（如容器 cgroup 读失败），看门狗据此降级
        （退到 idle 兜底判据）。本地=宿主进程 RSS/total；容器=容器内存/limit。
        """

    async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
        """采样单个工作单元的内存占用（RSS 字节）。

        默认实现返回 None（后端不支持单进程采样时降级）。看门狗据此判断
        某个具体进程是否自己吃内存过多——比 sample_memory(系统整体水位)
        对单进程失控更灵敏：31GB 宿主上单进程吃 2GB 只占 6% 触发不了
        系统水位，但该进程自身 RSS 已远超其应有上限，应判为失控。
        本地用 psutil 查单进程 RSS；容器读 /proc/<pid>/status 的 VmRSS。
        返回 None 表示采样不可用。
        """
        return None
