"""Trimmed 0.1 ``core.states`` shim — exposes only ``ExecutionStatus``.

The full 0.1 package also defines lifecycle/event/machine helpers, but those
are not imported by any of the four MCP sidecar tools, so they are omitted to
avoid pulling in extra dependencies.
"""

from core.states.execution import EXECUTION_TRANSITIONS, ExecutionStatus

__all__ = ["ExecutionStatus", "EXECUTION_TRANSITIONS"]
