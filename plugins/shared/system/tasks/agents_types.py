"""AgentLevel 枚举——从 agents/types.py 扁平化复制。

迁移适配：原 from agents.types import AgentLevel → from agents_types import AgentLevel
"""
from __future__ import annotations

from enum import Enum


class AgentLevel(Enum):
    """Agent 层级枚举。"""

    L1_MAIN = "L1"
    L2_SUBTASK = "L2"
    L3_ATOMIC = "L3"
