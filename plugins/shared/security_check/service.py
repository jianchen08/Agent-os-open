"""security_check 的交互异常类型。

交互服务本身已收口到 human_interaction_tool 插件（经 human-interaction
capability 调用），本模块只保留 security_check 内部 catch 用的异常类。
"""


class InteractionTimeoutError(Exception):
    """交互超时异常。"""

    def __init__(self, request_id: str, timeout: float):
        self.request_id = request_id
        self.timeout = timeout
        super().__init__(f"交互超时: {request_id} (超时时间: {timeout}秒)")


class InteractionCancelledError(Exception):
    """交互取消异常。"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互取消: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)


class InteractionDeniedError(Exception):
    """交互拒绝异常。"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互拒绝: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)
