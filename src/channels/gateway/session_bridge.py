"""跨通道会话状态桥接。

通过 unified_user_id 将不同通道的用户映射到同一会话，
支持用户在飞书、钉钉等通道间无缝切换，保持会话上下文连续。

核心方法：
- get_or_create_session: 获取或创建跨通道会话
- get_active_channel: 获取用户当前活跃通道
- switch_channel: 切换用户活跃通道
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionBridge:
    """跨通道会话状态桥接。

    维护 unified_user_id → session_id 的映射，确保同一用户
    在不同通道上共享同一会话上下文。

    持久化采用 JSON 文件存储，与现有 SessionService 的模式一致。

    Example::

        bridge = SessionBridge(storage_path=Path("./data/sessions"))
        session_id = bridge.get_or_create_session("feishu:ou_001", "feishu")
        bridge.switch_channel("feishu:ou_001", "dingtalk")
    """

    def __init__(self, storage_path: Path | None = None) -> None:
        """初始化会话桥接。

        Args:
            storage_path: 会话映射持久化目录，None 则仅内存模式
        """
        self._storage_path = storage_path
        # unified_user_id → session_id
        self._user_sessions: dict[str, str] = {}
        # unified_user_id → active channel_type
        self._active_channels: dict[str, str] = {}
        # 从持久化存储恢复
        self._load()

    def get_or_create_session(
        self,
        unified_user_id: str,
        channel_type: str,
    ) -> str:
        """获取或创建跨通道会话。

        如果用户已有会话，返回已有 session_id；否则创建新会话。
        首次注册时记录活跃通道。

        Args:
            unified_user_id: 跨通道统一用户 ID
            channel_type: 当前通道类型

        Returns:
            session_id 字符串
        """
        if unified_user_id in self._user_sessions:
            session_id = self._user_sessions[unified_user_id]
            logger.debug("Session found for user %s: %s", unified_user_id, session_id)
            return session_id

        # 创建新会话
        session_id = uuid.uuid4().hex[:12]
        self._user_sessions[unified_user_id] = session_id

        # 首次注册，设置活跃通道
        if unified_user_id not in self._active_channels:
            self._active_channels[unified_user_id] = channel_type

        self._persist()
        logger.info(
            "Session created: user=%s, session=%s, channel=%s",
            unified_user_id,
            session_id,
            channel_type,
        )
        return session_id

    def get_active_channel(self, unified_user_id: str) -> str:
        """获取用户当前活跃通道。

        Args:
            unified_user_id: 跨通道统一用户 ID

        Returns:
            活跃通道类型字符串，未知用户返回空字符串
        """
        return self._active_channels.get(unified_user_id, "")

    def switch_channel(self, unified_user_id: str, new_channel_type: str) -> None:
        """切换用户活跃通道。

        Args:
            unified_user_id: 跨通道统一用户 ID
            new_channel_type: 新的活跃通道类型
        """
        if unified_user_id not in self._user_sessions:
            logger.warning("Cannot switch channel for unknown user: %s", unified_user_id)
            return

        old_channel = self._active_channels.get(unified_user_id, "")
        self._active_channels[unified_user_id] = new_channel_type
        self._persist()
        logger.info(
            "Channel switched: user=%s, %s → %s",
            unified_user_id,
            old_channel,
            new_channel_type,
        )

    # ── 持久化 ──────────────────────────────────────────

    def _persist(self) -> None:
        """将会话映射持久化到 JSON 文件。"""
        if self._storage_path is None:
            return

        self._storage_path.mkdir(parents=True, exist_ok=True)
        state_file = self._storage_path / "session_bridge_state.json"
        data = {
            "user_sessions": self._user_sessions,
            "active_channels": self._active_channels,
        }
        try:
            state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("Failed to persist session bridge state: %s", exc)

    def _load(self) -> None:
        """从 JSON 文件恢复会话映射。"""
        if self._storage_path is None:
            return

        state_file = self._storage_path / "session_bridge_state.json"
        if not state_file.exists():
            return

        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            self._user_sessions = data.get("user_sessions", {})
            self._active_channels = data.get("active_channels", {})
            logger.info("Session bridge restored: %d users", len(self._user_sessions))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load session bridge state: %s", exc)
