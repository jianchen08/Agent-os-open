"""触发器类型定义。

定义运行时触发器所需的数据类型，与 evolution.types 兼容。

暴露接口：
- TriggerEvent: 触发事件数据类
- TriggerResult: 触发结果数据类
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evolution.types import EvolutionResult


@dataclass
class TriggerEvent:
    """触发事件。

    记录一次能力缺口触发信号的完整信息，用于审计和事件广播。

    Attributes:
        trigger_id: 触发事件唯一标识（自动生成）
        trigger_type: 触发类型（tool_not_found / capability_gap / manual）
        capability: 缺失的能力描述
        context: 附加上下文信息
        timestamp: ISO 8601 时间戳
        metadata: 扩展元数据
    """

    trigger_type: str
    capability: str
    context: dict[str, Any]
    timestamp: str
    trigger_id: str = field(default_factory=lambda: f"trig_{uuid.uuid4().hex[:12]}")
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化的字典。

        Returns:
            包含所有字段的字典
        """
        return {
            "trigger_id": self.trigger_id,
            "trigger_type": self.trigger_type,
            "capability": self.capability,
            "context": self.context,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class TriggerResult:
    """触发结果。

    描述一次触发尝试的结果，包括是否实际触发了进化流程。

    Attributes:
        triggered: 是否实际触发了进化流程
        evolution_result: 进化引擎返回的结果（未触发时为 None）
        message: 结果描述信息
        suggestion: 建议模式下的建议内容
    """

    triggered: bool
    evolution_result: EvolutionResult | None
    message: str
    suggestion: str | None = None

    @staticmethod
    def not_triggered(message: str) -> TriggerResult:
        """创建未触发的结果。

        Args:
            message: 未触发原因

        Returns:
            未触发的 TriggerResult
        """
        return TriggerResult(
            triggered=False,
            evolution_result=None,
            message=message,
        )

    @staticmethod
    def suggest(message: str, suggestion: str) -> TriggerResult:
        """创建建议模式的结果。

        Args:
            message: 描述信息
            suggestion: 建议内容

        Returns:
            建议模式的 TriggerResult
        """
        return TriggerResult(
            triggered=False,
            evolution_result=None,
            message=message,
            suggestion=suggestion,
        )


def make_timestamp() -> str:
    """生成当前 UTC 时间戳字符串。

    Returns:
        ISO 8601 格式的时间戳
    """
    return datetime.now(timezone.utc).isoformat()
