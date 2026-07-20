"""
核心异常基类 + Cost Control 模块异常定义

从 0.1 src/core/exceptions/base.py 和 src/core/exceptions/cost_control.py 合并提取。
异常层次结构保持不变，仅合并到单文件以适配平铺目录结构。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BaseAppException(Exception):
    """应用异常基类

    所有自定义异常的基类，提供统一的错误信息结构。

    Attributes:
        message: 错误消息
        code: 错误码
        details: 额外的错误详情字典
        cause: 原始异常（可选）
    """

    def __init__(
        self,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ):
        """初始化基础异常

        Args:
            message: 错误消息
            code: 错误码（可选，默认使用类名）
            details: 额外的错误详情（可选）
            cause: 原始异常（可选）
        """
        self.message = message
        self.code = code or self._default_code()
        self.details = details or {}
        self.cause = cause
        super().__init__(message)

        # 记录异常日志
        self._log_exception()

    def _default_code(self) -> str:
        """生成默认错误码（基于类名）"""
        class_name = self.__class__.__name__
        # 移除 Exception 后缀并转为大写
        if class_name.endswith("Exception"):
            class_name = class_name[:-9]
        return class_name.upper()

    def _log_exception(self):
        """记录异常日志"""
        # 子类可以覆盖此方法以自定义日志记录

    def __str__(self) -> str:
        """返回异常的字符串表示"""
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        """返回异常的详细表示"""
        return f"{self.__class__.__name__}(message={self.message!r}, code={self.code!r}, details={self.details!r})"

    def to_dict(self) -> dict[str, Any]:
        """将异常转换为字典格式

        Returns:
            包含错误信息的字典
        """
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "type": self.__class__.__name__,
        }


class DomainException(BaseAppException):
    """域异常基类

    用于表示业务逻辑中的错误，这些错误是预期的和可恢复的。
    """

    def _log_exception(self):
        """记录域异常为警告级别"""
        logger.warning(
            f"[{self.code}] {self.message}",
            extra={"details": self.details, "exception_type": self.__class__.__name__},
        )


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
