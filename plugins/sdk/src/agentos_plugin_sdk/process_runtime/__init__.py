"""进程运行时共享包：长进程代持 + 隔离环境三后端执行。

自 plugins/shared/bash/ 迁入（工具隔离声明化 Slice 2，
ADR 2026-10-21-tool-isolation-capability-declaration）——bash 工具与任意
声明 isolation_capable 的插件工具消费同一运行时单源。

对外形状对齐官方 MCP Tasks（SEP-1686 Final，状态机 submitted→working→
input_required→completed/failed/cancelled）：句柄立即返回不阻塞（call-now-
fetch-later），状态查询/结果获取/交互输入/取消一一对应。Tasks API 见
EnvProcessRuntime；实现载体是 ProcessManager（pid 句柄 + 日志真理源 +
看门狗孤儿回收）。
"""

from agentos_plugin_sdk.process_runtime.backends import (
    ContainerProcessBackend,
    LocalProcessBackend,
    WslNativeProcessBackend,
)
from agentos_plugin_sdk.process_runtime.encoding import EncodingHandler
from agentos_plugin_sdk.process_runtime.input_handler import InputHandler
from agentos_plugin_sdk.process_runtime.log_compressor import LogCompressor
from agentos_plugin_sdk.process_runtime.manager import (
    BackendUnhealthyError,
    ProcessLogReadError,
    ProcessManager,
    SpawnFirstLineTimeoutError,
)
from agentos_plugin_sdk.process_runtime.runtime import (
    TERMINAL_TASK_STATUSES,
    EnvProcessRuntime,
    TaskStateError,
    TaskStatus,
)
from agentos_plugin_sdk.process_runtime.shell_route import (
    is_compound_command,
    is_wsl_command,
    spawn_local_process,
)
from agentos_plugin_sdk.process_runtime.types import (
    BashAction,
    LogCompressorConfig,
    OutputSummary,
    OutputType,
    ProcessBackend,
    ProcessInfo,
    WorkUnit,
)

__all__ = [
    "BackendUnhealthyError",
    "BashAction",
    "ContainerProcessBackend",
    "EncodingHandler",
    "EnvProcessRuntime",
    "InputHandler",
    "LocalProcessBackend",
    "WslNativeProcessBackend",
    "LogCompressor",
    "LogCompressorConfig",
    "OutputSummary",
    "OutputType",
    "ProcessBackend",
    "ProcessInfo",
    "ProcessLogReadError",
    "ProcessManager",
    "SpawnFirstLineTimeoutError",
    "TaskStateError",
    "TaskStatus",
    "TERMINAL_TASK_STATUSES",
    "WorkUnit",
    "is_compound_command",
    "is_wsl_command",
    "spawn_local_process",
]
