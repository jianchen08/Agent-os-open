"""工具结果类型——与 0.1 ToolResult 结构对齐。

[来源: src/core/results/tool.py ToolExecutionResult]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """工具执行结果。

    与 0.1 的 ToolResult 结构对齐：
    - success: 是否成功
    - output: 输出数据
    - error: 错误信息（success=False 时有值）
    - metadata: 附加元数据
    """

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def success_result(cls, output: dict[str, Any], **metadata: Any) -> ToolResult:
        """创建成功结果。"""
        return cls(success=True, output=output, metadata=metadata)

    @classmethod
    def failure_result(cls, error: str, **metadata: Any) -> ToolResult:
        """创建失败结果。"""
        return cls(success=False, error=error, metadata=metadata)
