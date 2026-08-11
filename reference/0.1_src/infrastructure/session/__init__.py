"""会话管理模块。

提供统一的会话生命周期管理，供 CLI 和 Web 通道共用。
"""

from infrastructure.session.models import SessionModel
from infrastructure.session.session_service import SessionService

__all__ = ["SessionModel", "SessionService"]
