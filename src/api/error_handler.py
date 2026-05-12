"""
统一错误处理器

提供异常到 HTTP 响应的映射，确保所有错误都以统一格式返回。

设计原则:
1. 业务异常（DomainException）→ 4xx 客户端错误
2. 系统异常（SystemException）→ 5xx 服务器错误
3. 未捕获的异常 → 500 内部服务器错误
4. 所有错误都记录日志
5. 生产环境隐藏敏感信息
"""

import logging

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.api.errors import create_error_response
from src.config.settings import get_settings
from src.core.exceptions import (
    BaseAppException,
    BusinessRuleException,
    CacheException,
    ConfigurationException,
    ConflictException,
    DatabaseException,
    ExternalServiceException,
    NotFoundException,
    PermissionException,
    TimeoutException,
    ValidationException,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 异常到 HTTP 状态码和错误码的映射
# ============================================================================

ExceptionMapping = dict[type[Exception], tuple[int, str]]


# 默认的异常映射
DEFAULT_EXCEPTION_MAPPING: ExceptionMapping = {
    # 域异常（4xx 错误）
    ValidationException: (status.HTTP_400_BAD_REQUEST, "VAL_002"),
    NotFoundException: (status.HTTP_404_NOT_FOUND, "TASK_001"),
    ConflictException: (status.HTTP_409_CONFLICT, "TASK_003"),
    PermissionException: (status.HTTP_403_FORBIDDEN, "AUTH_006"),
    BusinessRuleException: (status.HTTP_400_BAD_REQUEST, "TASK_002"),
    # 系统异常（5xx 错误）
    DatabaseException: (status.HTTP_500_INTERNAL_SERVER_ERROR, "SYS_001"),
    CacheException: (status.HTTP_500_INTERNAL_SERVER_ERROR, "SYS_001"),
    ExternalServiceException: (status.HTTP_503_SERVICE_UNAVAILABLE, "SYS_002"),
    ConfigurationException: (status.HTTP_500_INTERNAL_SERVER_ERROR, "SYS_001"),
    TimeoutException: (status.HTTP_504_GATEWAY_TIMEOUT, "SYS_003"),
}


# ============================================================================
# 错误处理器
# ============================================================================


class ErrorHandler:
    """统一错误处理器

    提供异常到 HTTP 响应的转换逻辑。
    """

    def __init__(
        self,
        exception_mapping: ExceptionMapping | None = None,
        debug_mode: bool = False,
    ):
        """初始化错误处理器

        Args:
            exception_mapping: 自定义异常映射（可选）
            debug_mode: 是否为调试模式（可选）
        """
        self.mapping = exception_mapping or DEFAULT_EXCEPTION_MAPPING
        self.debug_mode = debug_mode

    def get_http_status_and_error_code(self, exc: Exception) -> tuple[int, str]:
        """获取异常对应的 HTTP 状态码和错误码

        Args:
            exc: 异常实例

        Returns:
            (HTTP状态码, 错误码) 元组
        """
        # 1. 检查自定义异常映射
        for exc_type, (status_code, error_code) in self.mapping.items():
            if isinstance(exc, exc_type):
                return status_code, error_code

        # 2. 检查是否是 BaseAppException
        if isinstance(exc, BaseAppException):
            # 根据异常类型推断状态码
            if isinstance(exc, (ValidationException, BusinessRuleException)):
                return status.HTTP_400_BAD_REQUEST, exc.code
            elif isinstance(exc, NotFoundException):
                return status.HTTP_404_NOT_FOUND, exc.code
            elif isinstance(exc, ConflictException):
                return status.HTTP_409_CONFLICT, exc.code
            elif isinstance(exc, PermissionException):
                return status.HTTP_403_FORBIDDEN, exc.code
            elif isinstance(exc, TimeoutException):
                return status.HTTP_504_GATEWAY_TIMEOUT, exc.code
            else:  # SystemException
                return status.HTTP_500_INTERNAL_SERVER_ERROR, exc.code

        # 3. 检查是否是 HTTPException（FastAPI 内置）
        if isinstance(exc, HTTPException):
            return exc.status_code, f"HTTP_{exc.status_code}"

        # 4. 检查是否是 ValidationError
        if isinstance(exc, (RequestValidationError, ValidationError)):
            return status.HTTP_422_UNPROCESSABLE_CONTENT, "VAL_001"

        # 5. 默认返回 500
        return status.HTTP_500_INTERNAL_SERVER_ERROR, "SYS_001"

    def get_error_message(self, exc: Exception, error_code: str) -> str:
        """获取用户友好的错误消息

        Args:
            exc: 异常实例
            error_code: 错误码

        Returns:
            用户友好的错误消息
        """
        # 优先使用异常自身的消息
        if isinstance(exc, BaseAppException):
            return exc.message

        # 使用预定义的错误消息
        from src.api.errors import get_error_message

        predefined_msg = get_error_message(error_code)
        if predefined_msg != "未知错误":
            return predefined_msg

        # 最后使用异常的消息
        return str(exc)

    def get_error_detail(self, exc: Exception) -> str | None:
        """获取详细的错误信息（仅用于开发环境）

        Args:
            exc: 异常实例

        Returns:
            详细错误信息（如果允许）
        """
        if not self.debug_mode:
            return None

        # BaseAppException 的详细信息
        if isinstance(exc, BaseAppException):
            detail = f"{exc.__class__.__name__}: {exc.message}"
            if exc.details:
                detail += f" | Details: {exc.details}"
            if exc.cause:
                cause_str = f"{type(exc.cause).__name__}: {str(exc.cause)}"
                detail += f" | Cause: {cause_str}"
            return detail

        # 其他异常的详细信息
        return f"{type(exc).__name__}: {str(exc)}"

    def get_validation_errors(self, exc: Exception) -> dict | None:
        """提取验证错误详情

        Args:
            exc: 异常实例

        Returns:
            验证错误字典（如果有）
        """
        if isinstance(exc, RequestValidationError):
            errors = {}
            for error in exc.errors():
                field = ".".join(str(loc) for loc in error["loc"])
                errors[field] = error["msg"]
            return errors

        if isinstance(exc, ValidationError):
            return exc.errors()

        # BaseAppException 的 details 字段
        if isinstance(exc, BaseAppException) and exc.details:
            return exc.details

        return None


def create_error_response_from_exception(
    exc: Exception,
    trace_id: str,
    request_path: str | None = None,
    error_handler: ErrorHandler | None = None,
) -> JSONResponse:
    """从异常创建错误响应

    Args:
        exc: 异常实例
        trace_id: 链路追踪 ID
        request_path: 请求路径（可选）
        error_handler: 错误处理器（可选）

    Returns:
        JSONResponse 实例
    """
    if error_handler is None:
        error_handler = ErrorHandler()

    # 获取 HTTP 状态码和错误码
    status_code, error_code = error_handler.get_http_status_and_error_code(exc)

    # 获取错误消息
    message = error_handler.get_error_message(exc, error_code)

    # 获取详细错误信息
    detail = error_handler.get_error_detail(exc)

    # 获取验证错误
    errors = error_handler.get_validation_errors(exc)

    # 创建错误响应
    error_response = create_error_response(
        code=error_code,
        trace_id=trace_id,
        detail=detail,
        errors=errors,
        message=message,
    )

    return JSONResponse(
        status_code=status_code,
        content=error_response.model_dump(mode="json"),
    )


# ============================================================================
# 全局异常处理器（用于 FastAPI）
# ============================================================================


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """全局异常处理器

    处理所有未捕获的异常，确保返回统一格式的错误响应。

    Args:
        request: 请求对象
        exc: 异常实例

    Returns:
        JSONResponse 实例
    """
    trace_id = getattr(request.state, "trace_id", "unknown")
    request_path = str(request.url.path)

    # 记录错误日志
    exc_type = type(exc).__name__
    logger.error(
        f"未捕获的异常 | trace_id={trace_id} | path={request_path} | "
        f"type={exc_type} | message={str(exc)}",
        exc_info=True,
    )

    # 从配置读取 debug_mode
    settings = get_settings()
    error_handler = ErrorHandler(debug_mode=settings.debug)
    return create_error_response_from_exception(
        exc=exc,
        trace_id=trace_id,
        request_path=request_path,
        error_handler=error_handler,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """HTTP 异常处理器

    处理 FastAPI 的 HTTPException。

    Args:
        request: 请求对象
        exc: HTTPException 实例

    Returns:
        JSONResponse 实例
    """
    trace_id = getattr(request.state, "trace_id", "unknown")
    request_path = str(request.url.path)

    # 记录日志
    status = exc.status_code
    logger.warning(
        f"HTTP 异常 | trace_id={trace_id} | path={request_path} | "
        f"status={status} | detail={exc.detail}"
    )

    # 创建错误响应
    error_code = f"HTTP_{exc.status_code}"
    error_response = create_error_response(
        code=error_code,
        trace_id=trace_id,
        detail=str(exc.detail),
        message=exc.detail if isinstance(exc.detail, str) else "请求错误",
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=error_response.model_dump(mode="json"),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """验证异常处理器

    处理请求验证错误。

    Args:
        request: 请求对象
        exc: RequestValidationError 实例

    Returns:
        JSONResponse 实例
    """
    trace_id = getattr(request.state, "trace_id", "unknown")
    request_path = str(request.url.path)

    # 记录日志
    errors_list = exc.errors()
    logger.warning(
        f"验证失败 | trace_id={trace_id} | path={request_path} | errors={errors_list}"
    )

    # 提取字段错误
    field_errors = {}
    for error in exc.errors():
        field = ".".join(str(loc) for loc in error["loc"])
        field_errors[field] = error["msg"]

    # 创建错误响应
    error_response = create_error_response(
        code="VAL_001",
        trace_id=trace_id,
        errors=field_errors,
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=error_response.model_dump(mode="json"),
    )


# ============================================================================
# 辅助函数
# ============================================================================


def setup_exception_handlers(app):
    """设置全局异常处理器

    Args:
        app: FastAPI 应用实例
    """
    from fastapi import FastAPI

    if not isinstance(app, FastAPI):
        err_msg = "app 必须是 FastAPI 实例"
        raise ValueError(err_msg)

    # 注册异常处理器
    app.add_exception_handler(Exception, global_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)


def raise_http_exception_from_app_exception(
    exc: BaseAppException,
) -> None:
    """将应用异常转换为 HTTP 异常并抛出

    用于在服务层抛出异常后，在路由层转换为 HTTP 异常。

    Args:
        exc: 应用异常实例

    Raises:
        HTTPException: 转换后的 HTTP 异常
    """
    error_handler = ErrorHandler()
    status_code, error_code = error_handler.get_http_status_and_error_code(exc)
    message = error_handler.get_error_message(exc, error_code)

    details_dict = exc.details if isinstance(exc, BaseAppException) else {}
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": error_code,
            "message": message,
            "details": details_dict,
        },
    )
