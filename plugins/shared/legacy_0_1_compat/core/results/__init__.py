"""Trimmed 0.1 ``core.results`` shim.

Only the result classes the four MCP sidecar tools actually use are exposed:
``ExecutionResult`` (base) and ``ToolExecutionResult``. The agent /
evaluation / tool_call submodules are intentionally NOT imported — they pull
in large dependency chains the sidecars do not need.
"""

from core.results.base import ExecutionResult
from core.results.tool import ToolExecutionResult

__all__ = [
    "ExecutionResult",
    "ToolExecutionResult",
]
