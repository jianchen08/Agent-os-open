"""
统一错误上报器

暴露接口：
- report_error(error_code: str, message: str, context: dict[str, Any] | None, severity: ErrorSeverity, exception: Exception | None) -> str：report_error功能
- record_metric(metric_name: str, value: float, tags: dict[str, str])：record_metric功能
- record(self, metric_name: str, value: float, tags: dict[str, str])：record功能
- get_metrics(self, metric_name: str | None, tags: dict[str, str] | None) -> dict[str, Any]：get_metrics功能
- generate_trace_id() -> str：generate_trace_id功能
- report(error_code: str, message: str, context: dict[str, Any] | None, severity: ErrorSeverity, exception: Exception | None, trace_id: str | None) -> str：report功能
- record_metric(metric_name: str, value: float, tags: dict[str, str])：record_metric功能
- get_metrics(metric_name: str | None, tags: dict[str, str] | None) -> dict[str, Any]：get_metrics功能
- get_error_counts() -> dict[str, int]：get_error_counts功能
- get_error_count(error_code: str) -> int：get_error_count功能
- reset_metrics()：reset_metrics功能
- ErrorSeverity：ErrorSeverity类
- ErrorCategory：ErrorCategory类
- ErrorMetrics：ErrorMetrics类
- ErrorReporter：ErrorReporter类
"""

import logging
import time
import traceback
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class ErrorSeverity(str, Enum):
    """错误严重程度"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ErrorCategory(str, Enum):
    """错误类别"""

    WS = "WS"  # WebSocket
    API = "API"  # REST API
    TOOL = "TOOL"  # 工具
    DB = "DB"  # 数据库
    MEM = "MEM"  # 记忆
    AUTH = "AUTH"  # 认证
    VAL = "VAL"  # 验证
    SYS = "SYS"  # 系统
    LLM = "LLM"  # LLM


class ErrorMetrics:
    """错误指标统计"""

    def __init__(self):
        self._metrics: dict[str, list] = {}

    def record(self, metric_name: str, value: float, tags: dict[str, str]) -> None:
        """记录指标"""
        key = f"{metric_name}:{sorted(tags.items())}"
        if key not in self._metrics:
            self._metrics[key] = []

        self._metrics[key].append(
            {"value": value, "tags": tags, "timestamp": time.time()}
        )

        # 保留最近 1000 条记录
        if len(self._metrics[key]) > 1000:
            self._metrics[key] = self._metrics[key][-1000:]

    def get_metrics(
        self, metric_name: str | None = None, tags: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """获取指标统计"""
        if metric_name is None:
            # 返回所有指标
            result = {}
            for key, records in self._metrics.items():
                name = key.split(":")[0]
                if name not in result:
                    result[name] = {"count": 0, "total": 0, "avg": 0}
                result[name]["count"] += len(records)
                result[name]["total"] += sum(r["value"] for r in records)
                result[name]["avg"] = (
                    result[name]["total"] / result[name]["count"]
                    if result[name]["count"] > 0
                    else 0
                )
            return result
        else:
            # 返回特定指标
            filtered_records = []
            for key, records in self._metrics.items():
                if key.startswith(metric_name):
                    if tags is None:
                        filtered_records.extend(records)
                    else:
                        for record in records:
                            if all(record["tags"].get(k) == v for k, v in tags.items()):
                                filtered_records.append(record)

            if not filtered_records:
                return {"count": 0, "total": 0, "avg": 0}

            return {
                "count": len(filtered_records),
                "total": sum(r["value"] for r in filtered_records),
                "avg": sum(r["value"] for r in filtered_records)
                / len(filtered_records),
            }


class ErrorReporter:
    """错误上报器

    提供统一的错误上报、记录和统计功能。
    """

    _metrics = ErrorMetrics()
    _error_counts: dict[str, int] = {}

    @staticmethod
    def generate_trace_id() -> str:
        """生成追踪 ID"""
        return str(uuid4())

    @staticmethod
    def report(
        error_code: str,
        message: str,
        context: dict[str, Any] | None = None,
        severity: ErrorSeverity = ErrorSeverity.ERROR,
        exception: Exception | None = None,
        trace_id: str | None = None,
    ) -> str:
        """上报错误"""
        if trace_id is None:
            trace_id = ErrorReporter.generate_trace_id()

        # 构建错误上下文（避免使用 LogRecord 保留字段）
        error_context = {
            "error_code": error_code,
            "error_message": message,  # 使用 error_message 而非 message
            "severity": severity.value,
            "timestamp": datetime.now(UTC).isoformat(),
            "trace_id": trace_id,
        }

        if context:
            error_context.update(context)

        if exception:
            error_context["exception_type"] = type(exception).__name__
            error_context["exception_message"] = str(exception)
            error_context["stack_trace"] = traceback.format_exc()

        # 根据严重程度选择日志级别
        if severity == ErrorSeverity.ERROR:
            logger.error(f"[{error_code}] {message}", extra=error_context)
        elif severity == ErrorSeverity.WARNING:
            logger.warning(f"[{error_code}] {message}", extra=error_context)
        else:
            logger.info(f"[{error_code}] {message}", extra=error_context)

        # 统计错误次数
        ErrorReporter._error_counts[error_code] = (
            ErrorReporter._error_counts.get(error_code, 0) + 1
        )

        # 记录错误指标
        category = error_code.split("_")[0] if "_" in error_code else "UNKNOWN"
        ErrorReporter._metrics.record(
            metric_name="error_count",
            value=1,
            tags={
                "error_code": error_code,
                "category": category,
                "severity": severity.value,
            },
        )

        return trace_id

    @staticmethod
    def record_metric(
        metric_name: str,
        value: float,
        tags: dict[str, str],
    ) -> None:
        """记录指标"""
        ErrorReporter._metrics.record(metric_name, value, tags)

        logger.debug(
            f"[指标] {metric_name}={value} tags={tags}",
        )

    @staticmethod
    def get_metrics(
        metric_name: str | None = None, tags: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """获取指标统计"""
        return ErrorReporter._metrics.get_metrics(metric_name, tags)

    @staticmethod
    def get_error_counts() -> dict[str, int]:
        """获取错误统计"""
        return ErrorReporter._error_counts.copy()

    @staticmethod
    def get_error_count(error_code: str) -> int:
        """获取特定错误码的发生次数"""
        return ErrorReporter._error_counts.get(error_code, 0)

    @staticmethod
    def reset_metrics() -> None:
        """重置所有指标(主要用于测试)"""
        ErrorReporter._metrics = ErrorMetrics()
        ErrorReporter._error_counts = {}


def report_error(
    error_code: str,
    message: str,
    context: dict[str, Any] | None = None,
    severity: ErrorSeverity = ErrorSeverity.ERROR,
    exception: Exception | None = None,
) -> str:
    """便捷函数：上报错误"""
    return ErrorReporter.report(error_code, message, context, severity, exception)


def record_metric(metric_name: str, value: float, tags: dict[str, str]) -> None:
    """便捷函数：记录指标"""
    ErrorReporter.record_metric(metric_name, value, tags)
