"""
0.2 本地结果类型（轻量兼容层）

sidecar 进程内自包含的 ToolResult——对齐 0.1 core.results.ExecutionResult
的字段语义（output/error/error_code/duration_ms/metadata + success 属性），
但只依赖 pydantic，不依赖 0.1 src 树。

暴露接口：
- ToolResult：工具执行结果
- create_success_result：创建成功结果
- create_failure_result：创建失败结果
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """工具执行结果（0.2 自包含版）。

    字段语义与 0.1 `core.results.ExecutionResult` 对齐：
    - output: 成功时的输出数据
    - error / error_code: 失败时的错误信息
    - duration_ms: 执行时长（毫秒）
    - metadata: 扩展元数据
    """

    output: Any | None = Field(default=None, description="输出数据")
    error: str | None = Field(default=None, description="错误信息")
    error_code: str | None = Field(default=None, description="错误代码")
    duration_ms: int | None = Field(default=None, description="执行时长（毫秒）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="扩展元数据")

    @property
    def success(self) -> bool:
        """是否成功（无错误即成功）。"""
        return self.error is None

    @classmethod
    def create_completed(cls, output: Any, metadata: dict[str, Any] | None = None) -> "ToolResult":
        """创建成功结果。"""
        return cls(output=output, metadata=metadata or {})

    @classmethod
    def create_failed(
        cls,
        error: str,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        """创建失败结果。"""
        return cls(error=error, error_code=error_code, metadata=metadata or {})


def create_success_result(
    data: Any = None,
    metadata: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> ToolResult:
    """创建成功结果。"""
    return ToolResult.create_completed(output=data, metadata=metadata).model_copy(
        update={"duration_ms": duration_ms}
    )


def create_failure_result(
    error: str,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """创建失败结果。"""
    return ToolResult.create_failed(error=error, error_code=error_code, metadata=metadata)
