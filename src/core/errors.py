"""
统一错误类型定义

暴露接口：
- get_error_message(error_code: str) -> str
- get_error_severity(error_code: str) -> ErrorSeverity
- get_suggested_action(error_code: str) -> str | None
- get_http_status(error_code: str) -> int
- is_retryable_error(error_code: str) -> bool
- create(cls, error_code: str, trace_id: str | None, details: dict[str, Any] | None, path: str | None, stack_trace: str | None) -> 'StandardError'
- to_http_response(self) -> tuple[int, 'StandardError']
- ErrorCode
- StandardError
- ErrorSeverity
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# 错误严重程度（原 error_reporter.py 内联）
# ============================================================================


class ErrorSeverity(str, Enum):
    """错误严重程度"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ============================================================================
# 错误码规范
# ============================================================================


class ErrorCode(str, Enum):
    """统一错误码定义

    格式: CATEGORY_SPECIFIC_CODE

    类别:
    - WS: WebSocket
    - API: REST API
    - TOOL: 工具
    - DB: 数据库
    - MEM: 记忆
    - AUTH: 认证
    - VAL: 验证
    - SYS: 系统
    - LLM: LLM

    子类别:
    - EXEC: 执行错误
    - CONN: 连接错误
    - AUTH: 认证错误
    - VAL: 验证错误
    - TIME: 超时错误
    - NOTF: 未找到错误
    - PERM: 权限错误
    """

    # WebSocket 错误 (1000-1999)
    WS_CONN_1001 = "WS_CONN_1001"  # 连接失败
    WS_CONN_1002 = "WS_CONN_1002"  # 连接超时
    WS_AUTH_1003 = "WS_AUTH_1003"  # 认证失败
    WS_AUTH_1004 = "WS_AUTH_1004"  # 令牌过期
    WS_MSG_1005 = "WS_MSG_1005"  # 消息格式错误
    WS_MSG_1006 = "WS_MSG_1006"  # 消息过大

    # API 错误 (2000-2999)
    API_AUTH_2001 = "API_AUTH_2001"  # 认证失败
    API_PERM_2002 = "API_PERM_2002"  # 权限不足
    API_VAL_2003 = "API_VAL_2003"  # 参数验证失败
    API_NOTF_2004 = "API_NOTF_2004"  # 资源未找到
    API_TIME_2005 = "API_TIME_2005"  # 请求超时

    # 工具错误 (3000-3999)
    TOOL_NOTF_3001 = "TOOL_NOTF_3001"  # 工具不存在
    TOOL_EXEC_3002 = "TOOL_EXEC_3002"  # 工具执行失败
    TOOL_VAL_3003 = "TOOL_VAL_3003"  # 参数验证失败
    TOOL_TIME_3004 = "TOOL_TIME_3004"  # 执行超时
    TOOL_PERM_3005 = "TOOL_PERM_3005"  # 需要审批
    TOOL_EXEC_3006 = "TOOL_EXEC_3006"  # 执行被取消

    # 数据库错误 (4000-4999)
    DB_CONN_4001 = "DB_CONN_4001"  # 连接失败
    DB_EXEC_4002 = "DB_EXEC_4002"  # 执行失败
    DB_TIME_4003 = "DB_TIME_4003"  # 查询超时

    # 记忆错误 (5000-5999)
    MEM_NOTF_5001 = "MEM_NOTF_5001"  # 记忆未找到
    MEM_EXEC_5002 = "MEM_EXEC_5002"  # 检索失败
    MEM_VAL_5003 = "MEM_VAL_5003"  # 格式错误

    # 认证错误 (6000-6999)
    AUTH_VAL_6001 = "AUTH_VAL_6001"  # 凭证无效
    AUTH_TIME_6002 = "AUTH_TIME_6002"  # 令牌过期
    AUTH_FAIL_6003 = "AUTH_FAIL_6003"  # 认证失败

    # 验证错误 (7000-7999)
    VAL_REQ_7001 = "VAL_REQ_7001"  # 缺少必需参数
    VAL_FMT_7002 = "VAL_FMT_7002"  # 格式错误
    VAL_RANGE_7003 = "VAL_RANGE_7003"  # 超出范围

    # 系统错误 (8000-8999)
    SYS_TIME_8001 = "SYS_TIME_8001"  # 系统超时
    SYS_LOAD_8002 = "SYS_LOAD_8002"  # 系统过载
    SYS_ERR_8003 = "SYS_ERR_8003"  # 内部错误

    # LLM 错误 (9000-9999)
    LLM_CONN_9001 = "LLM_CONN_9001"  # 连接失败
    LLM_EXEC_9002 = "LLM_EXEC_9002"  # 调用失败
    LLM_TIME_9003 = "LLM_TIME_9003"  # 调用超时


# ============================================================================
# 错误消息映射
# ============================================================================

ERROR_MESSAGES: dict[str, str] = {
    # WebSocket 错误
    ErrorCode.WS_CONN_1001: "WebSocket 连接失败",
    ErrorCode.WS_CONN_1002: "WebSocket 连接超时",
    ErrorCode.WS_AUTH_1003: "WebSocket 认证失败",
    ErrorCode.WS_AUTH_1004: "WebSocket 令牌已过期",
    ErrorCode.WS_MSG_1005: "WebSocket 消息格式错误",
    ErrorCode.WS_MSG_1006: "WebSocket 消息过大",
    # API 错误
    ErrorCode.API_AUTH_2001: "API 认证失败",
    ErrorCode.API_PERM_2002: "权限不足",
    ErrorCode.API_VAL_2003: "API 参数验证失败",
    ErrorCode.API_NOTF_2004: "资源未找到",
    ErrorCode.API_TIME_2005: "API 请求超时",
    # 工具错误
    ErrorCode.TOOL_NOTF_3001: "工具不存在",
    ErrorCode.TOOL_EXEC_3002: "工具执行失败",
    ErrorCode.TOOL_VAL_3003: "工具参数验证失败",
    ErrorCode.TOOL_TIME_3004: "工具执行超时",
    ErrorCode.TOOL_PERM_3005: "工具需要审批",
    ErrorCode.TOOL_EXEC_3006: "工具执行被取消",
    # 数据库错误
    ErrorCode.DB_CONN_4001: "数据库连接失败",
    ErrorCode.DB_EXEC_4002: "数据库执行失败",
    ErrorCode.DB_TIME_4003: "数据库查询超时",
    # 记忆错误
    ErrorCode.MEM_NOTF_5001: "记忆未找到",
    ErrorCode.MEM_EXEC_5002: "记忆检索失败",
    ErrorCode.MEM_VAL_5003: "记忆格式错误",
    # 认证错误
    ErrorCode.AUTH_VAL_6001: "认证凭证无效",
    ErrorCode.AUTH_TIME_6002: "认证令牌已过期",
    ErrorCode.AUTH_FAIL_6003: "认证失败",
    # 验证错误
    ErrorCode.VAL_REQ_7001: "缺少必需参数",
    ErrorCode.VAL_FMT_7002: "格式错误",
    ErrorCode.VAL_RANGE_7003: "参数超出允许范围",
    # 系统错误
    ErrorCode.SYS_TIME_8001: "系统超时",
    ErrorCode.SYS_LOAD_8002: "系统负载过高",
    ErrorCode.SYS_ERR_8003: "系统内部错误",
    # LLM 错误
    ErrorCode.LLM_CONN_9001: "LLM 连接失败",
    ErrorCode.LLM_EXEC_9002: "LLM 调用失败",
    ErrorCode.LLM_TIME_9003: "LLM 调用超时",
}


# ============================================================================
# 错误严重程度映射
# ============================================================================

ERROR_SEVERITY: dict[str, ErrorSeverity] = {
    # WebSocket 错误
    ErrorCode.WS_CONN_1001: ErrorSeverity.ERROR,
    ErrorCode.WS_CONN_1002: ErrorSeverity.WARNING,
    ErrorCode.WS_AUTH_1003: ErrorSeverity.ERROR,
    ErrorCode.WS_AUTH_1004: ErrorSeverity.WARNING,
    ErrorCode.WS_MSG_1005: ErrorSeverity.ERROR,
    ErrorCode.WS_MSG_1006: ErrorSeverity.ERROR,
    # API 错误
    ErrorCode.API_AUTH_2001: ErrorSeverity.ERROR,
    ErrorCode.API_PERM_2002: ErrorSeverity.ERROR,
    ErrorCode.API_VAL_2003: ErrorSeverity.ERROR,
    ErrorCode.API_NOTF_2004: ErrorSeverity.WARNING,
    ErrorCode.API_TIME_2005: ErrorSeverity.WARNING,
    # 工具错误
    ErrorCode.TOOL_NOTF_3001: ErrorSeverity.ERROR,
    ErrorCode.TOOL_EXEC_3002: ErrorSeverity.ERROR,
    ErrorCode.TOOL_VAL_3003: ErrorSeverity.ERROR,
    ErrorCode.TOOL_TIME_3004: ErrorSeverity.WARNING,
    ErrorCode.TOOL_PERM_3005: ErrorSeverity.INFO,
    ErrorCode.TOOL_EXEC_3006: ErrorSeverity.INFO,
    # 数据库错误
    ErrorCode.DB_CONN_4001: ErrorSeverity.ERROR,
    ErrorCode.DB_EXEC_4002: ErrorSeverity.ERROR,
    ErrorCode.DB_TIME_4003: ErrorSeverity.WARNING,
    # 记忆错误
    ErrorCode.MEM_NOTF_5001: ErrorSeverity.WARNING,
    ErrorCode.MEM_EXEC_5002: ErrorSeverity.ERROR,
    ErrorCode.MEM_VAL_5003: ErrorSeverity.ERROR,
    # 认证错误
    ErrorCode.AUTH_VAL_6001: ErrorSeverity.ERROR,
    ErrorCode.AUTH_TIME_6002: ErrorSeverity.WARNING,
    ErrorCode.AUTH_FAIL_6003: ErrorSeverity.ERROR,
    # 验证错误
    ErrorCode.VAL_REQ_7001: ErrorSeverity.ERROR,
    ErrorCode.VAL_FMT_7002: ErrorSeverity.ERROR,
    ErrorCode.VAL_RANGE_7003: ErrorSeverity.ERROR,
    # 系统错误
    ErrorCode.SYS_TIME_8001: ErrorSeverity.WARNING,
    ErrorCode.SYS_LOAD_8002: ErrorSeverity.WARNING,
    ErrorCode.SYS_ERR_8003: ErrorSeverity.ERROR,
    # LLM 错误
    ErrorCode.LLM_CONN_9001: ErrorSeverity.ERROR,
    ErrorCode.LLM_EXEC_9002: ErrorSeverity.ERROR,
    ErrorCode.LLM_TIME_9003: ErrorSeverity.WARNING,
}


# ============================================================================
# 建议操作映射
# ============================================================================

SUGGESTED_ACTIONS: dict[str, str] = {
    # WebSocket 错误
    ErrorCode.WS_CONN_1001: "请检查网络连接后重试",
    ErrorCode.WS_CONN_1002: "请稍后重试",
    ErrorCode.WS_AUTH_1003: "请重新登录",
    ErrorCode.WS_AUTH_1004: "正在自动刷新令牌...",
    ErrorCode.WS_MSG_1005: "请检查消息格式",
    ErrorCode.WS_MSG_1006: "请减少消息内容长度",
    # API 错误
    ErrorCode.API_AUTH_2001: "请重新登录",
    ErrorCode.API_PERM_2002: "您没有权限执行此操作",
    ErrorCode.API_VAL_2003: "请检查参数格式",
    ErrorCode.API_NOTF_2004: "请确认资源是否存在",
    ErrorCode.API_TIME_2005: "请稍后重试",
    # 工具错误
    ErrorCode.TOOL_NOTF_3001: "请确认工具名称是否正确",
    ErrorCode.TOOL_EXEC_3002: "请重试或联系管理员",
    ErrorCode.TOOL_VAL_3003: "请检查工具参数",
    ErrorCode.TOOL_TIME_3004: "请稍后重试",
    ErrorCode.TOOL_PERM_3005: "请在审批通过后重试",
    ErrorCode.TOOL_EXEC_3006: "工具执行已取消",
    # 数据库错误
    ErrorCode.DB_CONN_4001: "请稍后重试或联系管理员",
    ErrorCode.DB_EXEC_4002: "请稍后重试",
    ErrorCode.DB_TIME_4003: "请稍后重试",
    # 记忆错误
    ErrorCode.MEM_NOTF_5001: "请确认记忆是否存在",
    ErrorCode.MEM_EXEC_5002: "请重试",
    ErrorCode.MEM_VAL_5003: "请检查记忆格式",
    # 认证错误
    ErrorCode.AUTH_VAL_6001: "请检查用户名和密码",
    ErrorCode.AUTH_TIME_6002: "请重新登录",
    ErrorCode.AUTH_FAIL_6003: "请检查认证信息",
    # 验证错误
    ErrorCode.VAL_REQ_7001: "请提供所有必需参数",
    ErrorCode.VAL_FMT_7002: "请检查参数格式",
    ErrorCode.VAL_RANGE_7003: "请检查参数范围",
    # 系统错误
    ErrorCode.SYS_TIME_8001: "请稍后重试",
    ErrorCode.SYS_LOAD_8002: "请稍后重试",
    ErrorCode.SYS_ERR_8003: "请联系管理员",
    # LLM 错误
    ErrorCode.LLM_CONN_9001: "请检查 LLM 服务连接",
    ErrorCode.LLM_EXEC_9002: "请重试",
    ErrorCode.LLM_TIME_9003: "请稍后重试",
}


# ============================================================================
# HTTP 状态码映射
# ============================================================================

HTTP_STATUS_CODES: dict[str, int] = {
    # WebSocket 错误 (不直接映射到 HTTP)
    ErrorCode.WS_CONN_1001: 500,
    ErrorCode.WS_CONN_1002: 504,
    ErrorCode.WS_AUTH_1003: 401,
    ErrorCode.WS_AUTH_1004: 401,
    ErrorCode.WS_MSG_1005: 400,
    ErrorCode.WS_MSG_1006: 413,
    # API 错误
    ErrorCode.API_AUTH_2001: 401,
    ErrorCode.API_PERM_2002: 403,
    ErrorCode.API_VAL_2003: 400,
    ErrorCode.API_NOTF_2004: 404,
    ErrorCode.API_TIME_2005: 504,
    # 工具错误
    ErrorCode.TOOL_NOTF_3001: 404,
    ErrorCode.TOOL_EXEC_3002: 500,
    ErrorCode.TOOL_VAL_3003: 400,
    ErrorCode.TOOL_TIME_3004: 504,
    ErrorCode.TOOL_PERM_3005: 403,
    ErrorCode.TOOL_EXEC_3006: 400,
    # 数据库错误
    ErrorCode.DB_CONN_4001: 503,
    ErrorCode.DB_EXEC_4002: 500,
    ErrorCode.DB_TIME_4003: 504,
    # 记忆错误
    ErrorCode.MEM_NOTF_5001: 404,
    ErrorCode.MEM_EXEC_5002: 500,
    ErrorCode.MEM_VAL_5003: 400,
    # 认证错误
    ErrorCode.AUTH_VAL_6001: 401,
    ErrorCode.AUTH_TIME_6002: 401,
    ErrorCode.AUTH_FAIL_6003: 401,
    # 验证错误
    ErrorCode.VAL_REQ_7001: 400,
    ErrorCode.VAL_FMT_7002: 400,
    ErrorCode.VAL_RANGE_7003: 400,
    # 系统错误
    ErrorCode.SYS_TIME_8001: 504,
    ErrorCode.SYS_LOAD_8002: 503,
    ErrorCode.SYS_ERR_8003: 500,
    # LLM 错误
    ErrorCode.LLM_CONN_9001: 503,
    ErrorCode.LLM_EXEC_9002: 500,
    ErrorCode.LLM_TIME_9003: 504,
}


# ============================================================================
# 可重试错误
# ============================================================================

RETRYABLE_ERRORS = {
    ErrorCode.WS_CONN_1001,
    ErrorCode.WS_CONN_1002,
    ErrorCode.API_TIME_2005,
    ErrorCode.TOOL_TIME_3004,
    ErrorCode.DB_TIME_4003,
    ErrorCode.SYS_TIME_8001,
    ErrorCode.SYS_LOAD_8002,
    ErrorCode.LLM_TIME_9003,
}


# ============================================================================
# 统一错误响应格式
# ============================================================================


class StandardError(BaseModel):
    """标准错误响应格式

    所有 API 和 WebSocket 错误响应都应遵循此格式。
    """

    code: str = Field(..., description="错误码，格式: CATEGORY_SPECIFIC_CODE")
    message: str = Field(..., description="用户友好的错误消息")
    category: str = Field(..., description="错误类别")
    severity: str = Field(..., description="严重程度: info|warning|error")
    timestamp: datetime = Field(default_factory=datetime.now, description="错误发生时间")
    trace_id: str = Field(..., description="链路追踪 ID")
    path: str | None = Field(None, description="请求路径")
    details: dict[str, Any] | None = Field(None, description="详细信息(开发环境)")
    stack_trace: str | None = Field(None, description="堆栈跟踪(开发环境)")
    suggested_action: str | None = Field(None, description="建议操作")

    @classmethod
    def create(
        cls,
        error_code: str,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
        path: str | None = None,
        stack_trace: str | None = None,
    ) -> "StandardError":
        """创建标准错误响应"""
        # 生成 trace_id
        if trace_id is None:
            trace_id = str(uuid4())

        # 获取错误信息
        message = ERROR_MESSAGES.get(error_code, "未知错误")
        category = error_code.split("_", maxsplit=1)[0] if "_" in error_code else "UNKNOWN"
        severity = ERROR_SEVERITY.get(error_code, ErrorSeverity.ERROR).value
        suggested_action = SUGGESTED_ACTIONS.get(error_code)

        # 记录错误日志
        severity_level = ERROR_SEVERITY.get(error_code, ErrorSeverity.ERROR)
        if severity_level == ErrorSeverity.ERROR:
            logger.error("[%s] %s", error_code, message)
        elif severity_level == ErrorSeverity.WARNING:
            logger.warning("[%s] %s", error_code, message)
        else:
            logger.info("[%s] %s", error_code, message)

        return cls(
            code=error_code,
            message=message,
            category=category,
            severity=severity,
            trace_id=trace_id,
            path=path,
            details=details,
            stack_trace=stack_trace,
            suggested_action=suggested_action,
        )

    def to_http_response(self) -> tuple[int, "StandardError"]:
        """转换为 HTTP 响应"""
        status_code = HTTP_STATUS_CODES.get(self.code, 500)
        return status_code, self


# ============================================================================
# 辅助函数
# ============================================================================


def get_error_message(error_code: str) -> str:
    """获取错误消息"""
    return ERROR_MESSAGES.get(error_code, "未知错误")


def get_error_severity(error_code: str) -> ErrorSeverity:
    """获取错误严重程度"""
    return ERROR_SEVERITY.get(error_code, ErrorSeverity.ERROR)


def get_suggested_action(error_code: str) -> str | None:
    """获取建议操作"""
    return SUGGESTED_ACTIONS.get(error_code)


def get_http_status(error_code: str) -> int:
    """获取 HTTP 状态码"""
    return HTTP_STATUS_CODES.get(error_code, 500)


def is_retryable_error(error_code: str) -> bool:
    """判断错误是否可重试"""
    return error_code in RETRYABLE_ERRORS
