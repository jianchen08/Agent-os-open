"""
成本控制模块异常定义
"""
from src.core.exceptions import DomainException


class CostControlException(DomainException):
    pass


class BudgetExceededException(CostControlException):
    def __init__(self, message: str, current_usage: int, limit: int, limit_type: str = "task", details: dict = None):
        error_details = (details or {}).copy()
        error_details["current_usage"] = current_usage
        error_details["limit"] = limit
        error_details["limit_type"] = limit_type
        super().__init__(message, code="BUDGET_EXCEEDED", details=error_details)
        self.current_usage = current_usage
        self.limit = limit
        self.limit_type = limit_type


class QuotaExhaustedException(CostControlException):
    def __init__(self, message: str, usage_percent: float, quota_type: str = "daily", details: dict = None):
        error_details = (details or {}).copy()
        error_details["usage_percent"] = usage_percent
        error_details["quota_type"] = quota_type
        super().__init__(message, code="QUOTA_EXHAUSTED", details=error_details)
        self.usage_percent = usage_percent
        self.quota_type = quota_type
