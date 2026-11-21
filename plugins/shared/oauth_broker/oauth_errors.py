"""OAuth 错误值与共享常量——引擎（oauth_engine）与适配层（oauth_protocol）共同基面。

独立成模块只为避免 import 环（适配层引用引擎、引擎需要错误值），不是新的协议面。
错误是值：``OAuthError`` 携机器可读 ``code``（工具边界按 code 出失败信封），文本
给人看。错误码集合是外部行为契约（既有测试钉死），只增不改语义。
"""

from __future__ import annotations

#: 错误码（机器可读；工具边界按 code 出失败信封）
URL_INVALID = "URL_INVALID"
REQUEST_FAILED = "REQUEST_FAILED"
DISCOVERY_FAILED = "DISCOVERY_FAILED"
METADATA_INVALID = "METADATA_INVALID"
PKCE_UNSUPPORTED = "PKCE_UNSUPPORTED"
REGISTRATION_FAILED = "REGISTRATION_FAILED"
TOKEN_REQUEST_FAILED = "TOKEN_REQUEST_FAILED"
TOKEN_RESPONSE_INVALID = "TOKEN_RESPONSE_INVALID"

#: 端点请求缺省超时（秒）——发现 / 注册 / 令牌同档（既有口径）。
DEFAULT_TIMEOUT_SECS = 20.0


class OAuthError(Exception):
    """协议层失败（错误是值：``code`` 机器可读、文本给人看）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
