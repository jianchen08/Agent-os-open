"""UI Schema 模块的共享异常类型。

将业务异常从 channels 模块解耦，避免 channels <-> ui_schema 的循环导入。
"""

from __future__ import annotations

from typing import Any


class AutoCRUDError(Exception):
    """AutoCRUD 模块的业务异常。

    与 channels.api.deps.APIError 保持相同的属性接口，
    以便复用统一的异常处理器。

    Attributes:
        status_code: HTTP 状态码
        error_code: 业务错误码
        message: 用户可读的错误消息
        details: 附加详情字典
    """

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details or {}
        super().__init__(message)
