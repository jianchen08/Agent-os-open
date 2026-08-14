"""执行结果类型——插件共享层。

定义执行状态枚举与统一执行结果基类，供所有 MCP sidecar 工具共用。

来源：原 0.1 ``core/states/execution.py``（``ExecutionStatus``）+
``core/results/base.py``（``ExecutionResult``）+ ``core/results/tool.py``
（``ToolExecutionResult``）。这三个类被 task / triggers_ext / search /
task_submit 四个工具共同依赖，属于跨插件共享类型，故上提到 SDK 公共依赖层，
不再依赖 0.1 兼容 shim。

公共 API:
    ExecutionStatus: 统一执行状态枚举
    ExecutionResult: 执行结果基类（泛型，带状态/时间追踪/序列化）
    ToolExecutionResult: 工具执行结果（继承 ExecutionResult，带工具标识）
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from agentos_plugin_sdk.enum_utils import safe_enum_value

__all__ = [
    "EXECUTION_TRANSITIONS",
    "ExecutionResult",
    "ExecutionStatus",
    "ToolExecutionResult",
]

T = TypeVar("T")  # 输出数据类型

# slim（给 LLM 的）模式下，从 output 字典中剔除的大体积字段名集合。
# 这些字段是给前端 UI 渲染用的，不应整段回灌进 LLM 上下文：
# - old_content / new_content：file_write 的 diff 正文（老/新文件全文，
#   单次最多 _DIFF_CONTENT_MAX≈100KB），LLM 已知自己写了什么，回写原文纯属冗余。
# - diff_omitted：是否因体积超限省略了 diff 正文，对 LLM 无意义。
# 注意：仅当 output 为 dict 时生效；非 dict 类型（str/list 等）原样保留。
_SLIM_OUTPUT_EXCLUDE = frozenset({"old_content", "new_content", "diff_omitted"})


class ExecutionStatus(StrEnum):
    """统一执行状态。

    用于 Task、Agent、Workflow、Tool 等执行实体的状态管理。
    提供状态属性判断（终态、活跃、等待、成功、失败）。

    状态说明:
        - PENDING: 待执行，已创建但尚未开始
        - SCHEDULED: 已调度，已安排执行时间
        - RUNNING: 执行中
        - EVALUATING: 评估中
        - SUSPENDED: 暂停
        - BLOCKED: 阻塞
        - COMPLETED: 已完成（成功）
        - FAILED: 失败
        - CANCELLED: 已取消
        - TIMEOUT: 超时
    """

    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    EVALUATING = "evaluating"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        """是否为终态"""
        return self in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        }

    @property
    def is_active(self) -> bool:
        """是否为活跃状态（正在执行）"""
        return self in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.EVALUATING,
            ExecutionStatus.SCHEDULED,
        }

    @property
    def is_waiting(self) -> bool:
        """是否为等待状态"""
        return self in {
            ExecutionStatus.PENDING,
            ExecutionStatus.SUSPENDED,
            ExecutionStatus.BLOCKED,
        }

    @property
    def is_success(self) -> bool:
        """是否为成功终态"""
        return self == ExecutionStatus.COMPLETED

    @property
    def is_failure(self) -> bool:
        """是否为失败终态"""
        return self in {
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        }


# 执行状态转换规则（与 0.1 core/states/execution.py 保持一致）。
# 定义每个状态可以合法转换到的目标状态列表。
EXECUTION_TRANSITIONS: dict[ExecutionStatus, list[ExecutionStatus]] = {
    ExecutionStatus.PENDING: [
        ExecutionStatus.SCHEDULED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.SCHEDULED: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.RUNNING: [
        ExecutionStatus.EVALUATING,
        ExecutionStatus.SUSPENDED,
        ExecutionStatus.BLOCKED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.TIMEOUT,
    ],
    ExecutionStatus.EVALUATING: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.BLOCKED,
    ],
    ExecutionStatus.SUSPENDED: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.BLOCKED: [
        ExecutionStatus.RUNNING,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.COMPLETED: [],
    ExecutionStatus.FAILED: [
        ExecutionStatus.PENDING,
        ExecutionStatus.CANCELLED,
    ],
    ExecutionStatus.CANCELLED: [],
    ExecutionStatus.TIMEOUT: [
        ExecutionStatus.PENDING,
        ExecutionStatus.CANCELLED,
    ],
}


class ExecutionResult(BaseModel, Generic[T]):
    """执行结果基类。

    所有执行结果的统一基类，提供：
    - 统一的状态表示
    - 统一的时间追踪
    - 统一的错误处理
    - 统一的序列化方法

    Attributes:
        status: 执行状态
        output: 输出数据
        error: 错误信息
        error_code: 错误代码
        started_at: 开始时间
        completed_at: 完成时间
        duration_ms: 执行时长（毫秒）
        metadata: 扩展元数据
    """

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )

    # === 核心状态 ===
    status: ExecutionStatus = Field(default=ExecutionStatus.PENDING, description="执行状态")

    # === 输出数据 ===
    output: T | None = Field(default=None, description="输出数据")

    # === 错误信息 ===
    error: str | None = Field(default=None, description="错误信息")
    error_code: str | None = Field(default=None, description="错误代码")

    # === 时间追踪 ===
    started_at: datetime | None = Field(default=None, description="开始时间")
    completed_at: datetime | None = Field(default=None, description="完成时间")
    duration_ms: int | None = Field(default=None, description="执行时长（毫秒）")

    # === 元数据 ===
    metadata: dict[str, Any] = Field(default_factory=dict, description="扩展元数据")

    # === 便捷属性 ===

    @property
    def success(self) -> bool:
        """是否成功完成"""
        return self.status == ExecutionStatus.COMPLETED

    @property
    def is_failed(self) -> bool:
        """是否失败"""
        return self.status in (
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.CANCELLED,
        )

    @property
    def is_terminal(self) -> bool:
        """是否已终止（完成或失败）"""
        return self.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        )

    # === 工厂方法 ===

    @classmethod
    def create_running(cls, **kwargs: Any) -> Self:
        """创建运行中状态的结果"""
        return cls(status=ExecutionStatus.RUNNING, started_at=datetime.now(UTC), **kwargs)

    @classmethod
    def create_completed(cls, output: T, **kwargs: Any) -> Self:
        """创建成功完成的结果"""
        now = datetime.now(UTC)
        return cls(status=ExecutionStatus.COMPLETED, output=output, completed_at=now, **kwargs)

    @classmethod
    def create_failed(cls, error: str, error_code: str | None = None, **kwargs: Any) -> Self:
        """创建失败结果"""
        return cls(
            status=ExecutionStatus.FAILED, error=error, error_code=error_code, completed_at=datetime.now(UTC), **kwargs
        )

    # === 序列化方法 ===

    def to_dict(self, slim: bool = False) -> dict[str, Any]:  # noqa: PLR0912
        """转换为字典（统一序列化）

        Args:
            slim: 精简模式，仅保留 LLM 需要的字段。
                成功时省略 status/success/completed_at/started_at/duration_ms，
                仅保留 output 和有意义的 metadata。
                失败时保留 success/error/error_code。
        """
        if slim:
            result: dict[str, Any] = {}
            if not self.success:
                result["success"] = False
                if self.error:
                    result["error"] = self.error
                if self.error_code:
                    result["error_code"] = self.error_code
            else:
                if self.output is not None:
                    serialized = self._serialize_output()
                    # slim 模式剔除 output 内的大体积 diff 正文。
                    if isinstance(serialized, dict):
                        serialized = {k: v for k, v in serialized.items() if k not in _SLIM_OUTPUT_EXCLUDE}
                    result["output"] = serialized
                if self.metadata:
                    # slim 模式排除大体积字段，避免 base64 污染 LLM 文本上下文
                    _slim_exclude = {"action", "multimodal_content"}
                    non_excluded = {k: v for k, v in self.metadata.items() if k not in _slim_exclude}
                    if non_excluded:
                        result["metadata"] = non_excluded
            return result

        # 处理 status：由于 use_enum_values=True，status 可能已经是字符串
        status_value = safe_enum_value(self.status)

        result = {
            "status": status_value,
            "success": self.success,
        }
        if self.output is not None:
            result["output"] = self._serialize_output()
        if self.error:
            result["error"] = self.error
        if self.error_code:
            result["error_code"] = self.error_code
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        if self.started_at:
            result["started_at"] = self.started_at.isoformat()
        if self.completed_at:
            result["completed_at"] = self.completed_at.isoformat()
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    def _serialize_output(self) -> Any:
        """序列化输出（子类可覆盖）"""
        if isinstance(self.output, BaseModel):
            return self.output.model_dump()
        return self.output

    def calculate_duration(self) -> int | None:
        """计算执行时长"""
        if self.started_at and self.completed_at:
            delta = self.completed_at - self.started_at
            self.duration_ms = int(delta.total_seconds() * 1000)
            return self.duration_ms
        return None


class ToolExecutionResult(ExecutionResult[Any]):
    """工具执行结果。

    继承自 ExecutionResult 基类，添加工具特有字段。

    Attributes:
        tool_name: 工具名称
        tool_id: 工具 ID
        input_params: 输入参数
    """

    # 工具标识
    tool_name: str | None = Field(default=None, description="工具名称")
    tool_id: str | None = Field(default=None, description="工具 ID")

    # 输入参数
    input_params: dict[str, Any] = Field(default_factory=dict, description="输入参数")

    def to_dict(self, slim: bool = False) -> dict[str, Any]:
        """转换为字典

        Args:
            slim: 精简模式，省略 tool_name/tool_id/input_params 等冗余字段
        """
        result = super().to_dict(slim=slim)

        if not slim:
            if self.tool_name:
                result["tool_name"] = self.tool_name
            if self.tool_id:
                result["tool_id"] = self.tool_id
            if self.input_params:
                result["input_params"] = self.input_params

        return result
