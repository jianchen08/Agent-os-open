"""Trimmed 0.1 ``agents.types`` shim — exposes only the ``AgentLevel`` and
``AgentType`` enums.

The full 0.1 module defines a large ``AgentConfig`` dataclass plus many
sub-config dataclasses (ContextConfig, KnowledgeConfig, RuleReinforcement,
DeliverableSpec, MetricRef, AgentPluginsConfig, …) used by the agent loader /
registry. None of those are needed by the four MCP sidecar tools; only
``AgentLevel`` is referenced (by ``tasks.types.TaskModel`` and lazily by
``task_submit/tool.py``), so only the enums are mirrored here to keep the
compat surface minimal.
"""

from __future__ import annotations

from enum import Enum


class AgentLevel(Enum):
    """Agent 层级枚举（0.1 兼容）。"""

    L1_MAIN = "L1"
    L2_SUBTASK = "L2"
    L3_ATOMIC = "L3"


class AgentType(Enum):
    """Agent 类型枚举（0.1 兼容）。"""

    MAIN = "main"
    SPECIALIZED = "specialized"
    SYSTEM = "system"
