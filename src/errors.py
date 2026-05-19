"""错误处理与恢复模块（简化版）。

提供基础的错误处理、重试和恢复机制。

注意：本模块是历史遗留的简化错误系统。项目完整的统一错误系统位于
src/core/errors.py（含 StandardError、ErrorCode 等完整特性）。
本模块因测试文件 tests/test_state_evolution_levels.py 的引用而暂时保留，
后续应迁移引用并删除本文件。
"""

from __future__ import annotations

import time
from typing import Any, Callable


class AppError(Exception):
    """应用层基础异常。"""

    def __init__(self, message: str, code: str = "UNKNOWN", recoverable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": self.code,
            "recoverable": self.recoverable,
        }


class ConnectionError_(AppError):
    """连接相关错误。"""

    def __init__(self, message: str = "连接失败") -> None:
        super().__init__(message, code="CONNECTION_ERROR", recoverable=True)


class MessageValidationError(AppError):
    """消息验证错误。"""

    def __init__(self, message: str = "消息格式无效") -> None:
        super().__init__(message, code="VALIDATION_ERROR", recoverable=False)


class SessionNotFoundError(AppError):
    """会话未找到错误。"""

    def __init__(self, thread_id: str = "") -> None:
        super().__init__(
            f"会话未找到: {thread_id}",
            code="SESSION_NOT_FOUND",
            recoverable=False,
        )


class RetryPolicy:
    """重试策略。"""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential: bool = True,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential = exponential

    def get_delay(self, attempt: int) -> float:
        """计算第 N 次重试的等待时间。"""
        if self.exponential:
            delay = self.base_delay * (2 ** attempt)
        else:
            delay = self.base_delay
        return min(delay, self.max_delay)


class ErrorRecovery:
    """错误恢复处理器。"""

    def __init__(self, retry_policy: RetryPolicy | None = None) -> None:
        self.retry_policy = retry_policy or RetryPolicy()
        self._error_counts: dict[str, int] = {}
        self._last_errors: dict[str, AppError] = {}

    def record_error(self, error: AppError) -> None:
        """记录错误。"""
        self._error_counts[error.code] = self._error_counts.get(error.code, 0) + 1
        self._last_errors[error.code] = error

    def can_retry(self, error: AppError) -> bool:
        """判断错误是否可重试。"""
        if not error.recoverable:
            return False
        count = self._error_counts.get(error.code, 0)
        return count < self.retry_policy.max_retries

    async def execute_with_retry(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """带重试的执行函数。"""
        last_error: Exception | None = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                # 成功后清除错误计数
                if last_error and isinstance(last_error, AppError):
                    self._error_counts.pop(last_error.code, None)
                return result
            except AppError as e:
                last_error = e
                self.record_error(e)
                if not self.can_retry(e):
                    raise
                delay = self.retry_policy.get_delay(attempt)
                time.sleep(delay)
            except Exception as e:
                last_error = e
                if attempt >= self.retry_policy.max_retries:
                    raise
                delay = self.retry_policy.get_delay(attempt)
                time.sleep(delay)
        raise last_error  # type: ignore

    def get_error_count(self, code: str) -> int:
        """获取指定错误码的错误次数。"""
        return self._error_counts.get(code, 0)

    def get_last_error(self, code: str) -> AppError | None:
        """获取指定错误码的最后一次错误。"""
        return self._last_errors.get(code)

    def reset(self) -> None:
        """重置错误状态。"""
        self._error_counts.clear()
        self._last_errors.clear()
