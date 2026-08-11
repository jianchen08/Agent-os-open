"""
执行结果基类

暴露接口：
- success(self) -> bool：success功能
- is_failed(self) -> bool：is_failed功能
- is_terminal(self) -> bool：is_terminal功能
- create_running(cls) -> 'ExecutionResult[T]'：create_running功能
- create_completed(cls, output: T) -> 'ExecutionResult[T]'：create_completed功能
- create_failed(cls, error: str, error_code: str | None) -> 'ExecutionResult[T]'：create_failed功能
- to_dict(self) -> dict[str, Any]：to_dict功能
- calculate_duration(self) -> int | None：calculate_duration功能
- ExecutionResult：ExecutionResult类
"""

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from core.states import ExecutionStatus
from utils.enum_utils import safe_enum_value

T = TypeVar("T")  # 输出数据类型

# slim（给 LLM 的）模式下，从 output 字典中剔除的大体积字段名集合。
# 这些字段是给前端 UI 渲染用的，不应整段回灌进 LLM 上下文：
# - old_content / new_content：file_write 的 diff 正文（老/新文件全文，
#   单次最多 _DIFF_CONTENT_MAX≈100KB），LLM 已知自己写了什么，回写原文纯属冗余。
# - diff_omitted：是否因体积超限省略了 diff 正文，对 LLM 无意义。
# 注意：仅当 output 为 dict 时生效；非 dict 类型（str/list 等）原样保留。
_SLIM_OUTPUT_EXCLUDE = frozenset({"old_content", "new_content", "diff_omitted"})


class ExecutionResult(BaseModel, Generic[T]):
    """执行结果基类

    所有执行结果的统一基类，提供：
    - 统一的状态表示
    - 统一的时间追踪
    - 统一的错误处理
    - 统一的序列化方法

    设计决策：
    1. 使用 status 枚举而非 success 布尔值
       - 原因：状态更丰富，可表示 pending/running/completed/failed/cancelled/timeout
    2. 使用 output 而非 data/result
       - 原因：语义更清晰，表示"输出结果"
    3. 使用 duration_ms 统一时间单位
       - 原因：毫秒精度足够，避免浮点精度问题

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
    def create_running(cls, **kwargs: Any) -> "ExecutionResult[T]":
        """创建运行中状态的结果"""
        return cls(status=ExecutionStatus.RUNNING, started_at=datetime.now(UTC), **kwargs)

    @classmethod
    def create_completed(cls, output: T, **kwargs: Any) -> "ExecutionResult[T]":
        """创建成功完成的结果"""
        now = datetime.now(UTC)
        return cls(status=ExecutionStatus.COMPLETED, output=output, completed_at=now, **kwargs)

    @classmethod
    def create_failed(cls, error: str, error_code: str | None = None, **kwargs: Any) -> "ExecutionResult[T]":
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
                    # slim 模式剔除 output 内的大体积 diff 正文：
                    # old_content/new_content 是 file_write 供前端卡片渲染的完整文件
                    # 文本（老/新内容，单次最多 _DIFF_CONTENT_MAX），它们对 LLM 无信息
                    # 增益（模型刚发起这次写入，已知自己写了什么），但会让写入原文
                    # 整段回灌进上下文。此处只保留 added/removed 等统计量给 LLM。
                    # diff_omitted 同样无意义，一并剔除。
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
