"""Shell 命令执行工具。

核心业务逻辑从 0.1 src/tools/builtin/bash/ 迁移。

危险命令黑名单（punch C17 单一事实源）：直接从共享层 bash 插件导入
``bash.tool.DANGEROUS_PATTERNS``（SecurityChecker 分类为准），本文件不再
维护本地副本。语义随事实源统一：
- ``| bash`` / ``| sh`` 等管道到 shell 属 CAUTION（降级审批）而非硬拦——
  builtin 路径无审批管道，故不在此拦截（与 bash/tool.py 分层原则一致）；
- ``rm -rf`` / ``mkfs`` / ``dd if=`` 等不可逆灾难仍硬拦。

导入路径：server.py / tests/conftest.py 把 plugins/shared/tools 与
plugins/shared/tools/bash 注入 sys.path（bash.tool 以命名空间包导入，
其平铺依赖 bash_types 等经 bash 目录解析）。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from bash.tool import DANGEROUS_PATTERNS

from agentos_builtin_tools.result import ToolResult

BASH_EXECUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 Shell 命令"},
        "timeout": {"type": "integer", "description": "超时时间（秒），默认 30", "default": 30},
        "working_dir": {"type": "string", "description": "工作目录（可选）"},
        "action": {
            "type": "string",
            "enum": ["execute", "continue", "terminate", "input", "read_log"],
            "description": "操作类型",
            "default": "execute",
        },
    },
    "required": ["command"],
}

BASH_EXECUTE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "exit_code": {"type": "integer"},
        "pid": {"type": ["integer", "null"]},
    },
}


def _check_dangerous(command: str) -> str | None:
    """检查命令是否包含危险模式（单一事实源：bash.tool.DANGEROUS_PATTERNS）。

    匹配语义与 SecurityChecker.check 对齐：re.IGNORECASE（大小写不敏感）。

    Returns:
        匹配到的危险模式描述，None 表示安全。
    """
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"dangerous command detected: pattern={pattern}"
    return None


async def bash_execute(
    command: str,
    timeout: int = 30,
    working_dir: str | None = None,
    action: str = "execute",
) -> ToolResult:
    """执行 Shell 命令。

    安全检查：拦截危险命令（rm -rf /, mkfs, dd if=, fork bomb 等）。
    超时控制：默认 30 秒。
    """
    danger = _check_dangerous(command)
    if danger:
        return ToolResult.failure_result(danger, command=command)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
    except OSError as e:
        return ToolResult.failure_result(f"Failed to spawn process: {e}")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult.failure_result(
            f"Command timed out after {timeout}s",
            pid=proc.pid,
        )

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    exit_code = proc.returncode if proc.returncode is not None else -1

    return ToolResult(
        success=exit_code == 0,
        output={
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "pid": proc.pid,
        },
        error=stderr.strip() if exit_code != 0 and stderr.strip() else None,
    )
