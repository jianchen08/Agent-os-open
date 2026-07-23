"""Shell 命令执行工具。

核心业务逻辑从 0.1 src/tools/builtin/bash/ 迁移。
安全检查复用 0.1 的 SecurityChecker 模式。

[来源: src/tools/builtin/bash/tool.py]
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agentos_builtin_tools.result import ToolResult

# 危险命令正则（与 0.1 SecurityChecker.DANGEROUS_PATTERNS 对齐）
_DANGEROUS_PATTERNS: list[str] = [
    r"\brm\s+-rf\b",
    r"\brm\s+-rf\s+/",
    r"\|\s*bash\b",
    r"\|\s*sh\b",
    r"\|\s*zsh\b",
    r"\|\s*fish\b",
    r";\s*rm\b",
    r";\s*del\b",
    r";\s*format\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd[a-z]",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\bdel\s+/f\s+/s\s+/q\b",
    r"\brmdir\s+/s\s+/q\b",
    r"\bformat\s+[a-z]:",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
]

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
    """检查命令是否包含危险模式。

    Returns:
        匹配到的危险模式描述，None 表示安全。
    """
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, command):
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
