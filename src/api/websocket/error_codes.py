"""
WebSocket错误码定义

与前端WebSocketErrorCode保持一致，确保前后端错误处理统一
"""

from enum import IntEnum


class WebSocketErrorCode(IntEnum):
    """WebSocket错误码枚举

    与前端 frontend/src/constants/websocket.ts 中的 WebSocketErrorCode 保持一致
    """

    # 认证相关 (1000-1999)
    AUTH_FAILED = 1001  # 认证失败
    TOKEN_EXPIRED = 1002  # 令牌过期
    CONNECTION_LIMIT = 1003  # 连接数超限

    # 网络相关 (2000-2999)
    CONNECTION_LOST = 2001  # 连接丢失
    TIMEOUT = 2002  # 连接超时
    UNREACHABLE = 2003  # 服务端不可达

    # 服务端相关 (3000-3999)
    SERVER_ERROR = 3001  # 服务端内部错误
    RATE_LIMITED = 3002  # 请求频率限制
    MAINTENANCE = 3003  # 服务维护中

    # 消息相关 (4000-4999)
    MESSAGE_TOO_LARGE = 4001  # 消息过大
    INVALID_FORMAT = 4002  # 消息格式无效
    UNSUPPORTED_TYPE = 4003  # 不支持的消息类型


# 错误码与用户提示信息的映射表
ERROR_MESSAGES = {
    WebSocketErrorCode.AUTH_FAILED: {
        "message": "认证失败，请检查您的登录状态",
        "retryable": False,
        "action": "请重新登录",
    },
    WebSocketErrorCode.TOKEN_EXPIRED: {
        "message": "登录已过期，请重新登录",
        "retryable": False,
        "action": "请重新登录",
    },
    WebSocketErrorCode.CONNECTION_LIMIT: {
        "message": "连接数已达到上限",
        "retryable": False,
        "action": "请关闭其他标签页或稍后重试",
    },
    WebSocketErrorCode.CONNECTION_LOST: {
        "message": "网络连接已断开",
        "retryable": True,
        "action": "正在尝试重新连接...",
    },
    WebSocketErrorCode.TIMEOUT: {
        "message": "连接超时",
        "retryable": True,
        "action": "正在尝试重新连接...",
    },
    WebSocketErrorCode.UNREACHABLE: {
        "message": "无法连接到服务器",
        "retryable": True,
        "action": "请检查网络连接",
    },
    WebSocketErrorCode.SERVER_ERROR: {
        "message": "服务端内部错误",
        "retryable": True,
        "action": "正在尝试重新连接...",
    },
    WebSocketErrorCode.RATE_LIMITED: {
        "message": "请求过于频繁，请稍后再试",
        "retryable": False,
        "action": "请稍后再试",
    },
    WebSocketErrorCode.MAINTENANCE: {
        "message": "服务正在维护中",
        "retryable": False,
        "action": "请稍后再试",
    },
    WebSocketErrorCode.MESSAGE_TOO_LARGE: {
        "message": "消息内容过大",
        "retryable": False,
        "action": "请减少消息内容或分批发送",
    },
    WebSocketErrorCode.INVALID_FORMAT: {
        "message": "消息格式无效",
        "retryable": False,
        "action": "请检查消息格式",
    },
    WebSocketErrorCode.UNSUPPORTED_TYPE: {
        "message": "不支持的消息类型",
        "retryable": False,
        "action": "请使用支持的消息类型",
    },
}


def get_error_message(error_code: int) -> dict:
    """获取错误提示信息

    Args:
        error_code: 错误码

    Returns:
        包含 message, retryable, action 的字典
    """
    if error_code in ERROR_MESSAGES:
        return ERROR_MESSAGES[error_code]
    else:
        return {
            "message": "未知错误",
            "retryable": False,
            "action": "请稍后重试或联系管理员",
        }


def is_retryable_error(error_code: int) -> bool:
    """判断错误是否可重试

    Args:
        error_code: 错误码

    Returns:
        是否可重试
    """
    if error_code in ERROR_MESSAGES:
        return ERROR_MESSAGES[error_code]["retryable"]
    return False
