"""
Cost Control 模块异常定义
"""

from typing import Any

from core.exceptions.base import DomainException


class CostControlException(DomainException):
    """成本控制异常基类

    成本控制模块相关异常的基类。
    """

    pass


class BudgetExceededException(CostControlException):
    """预算超限异常

    Attributes:
        current_usage: 当前使用量
        limit: 限制值
        limit_type: 限制类型
    """

    def __init__(
        self,
        message: str,
        current_usage: int,
        limit: int,
        limit_type: str = "task",
        details: dict[str, Any] | None = None,
    ):
        """初始化预算超限异常

        Args:
            message: 错误消息
            current_usage: 当前使用量
            limit: 限制值
            limit_type: 限制类型
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["current_usage"] = current_usage
        error_details["limit"] = limit
        error_details["limit_type"] = limit_type
        super().__init__(message, code="BUDGET_EXCEEDED", details=error_details)
        self.current_usage = current_usage
        self.limit = limit
        self.limit_type = limit_type


class QuotaExhaustedException(CostControlException):
    """配额耗尽异常

    Attributes:
        usage_percent: 使用百分比
        quota_type: 配额类型
    """

    def __init__(
        self,
        message: str,
        usage_percent: float,
        quota_type: str = "daily",
        details: dict[str, Any] | None = None,
    ):
        """初始化配额耗尽异常

        Args:
            message: 错误消息
            usage_percent: 使用百分比
            quota_type: 配额类型
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["usage_percent"] = usage_percent
        error_details["quota_type"] = quota_type
        super().__init__(message, code="QUOTA_EXHAUSTED", details=error_details)
        self.usage_percent = usage_percent
        self.quota_type = quota_type
