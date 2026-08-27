"""AgentLevel 枚举——tasks 域自持的 agent 层级定义（主任务/子任务/原子任务）。"""
from __future__ import annotations

from enum import Enum


class AgentLevel(Enum):
    """Agent 层级枚举。"""

    L1_MAIN = "L1"
    L2_SUBTASK = "L2"
    L3_ATOMIC = "L3"
