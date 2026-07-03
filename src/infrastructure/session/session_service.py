"""会话服务 — 管理会话（标记）的创建、恢复、清空。

会话只是一个标记，记录管道 ID 的引用集合。
管道的创建和 ID 生成本身由管道引擎负责，不由此服务管理。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from infrastructure.session.models import SessionModel

logger = logging.getLogger(__name__)


class SessionService:
    """会话服务 — 管理 session 的创建和持久化。

    会话不生成 pipeline_id，只做引用收集。
    管道运行前/后通过 register_pipeline 将 pipeline_id 记录到会话。

    Usage::

        svc = SessionService(session_dir=path)
        session = await svc.create_or_restore(channel_type="cli")
        # 管道自己生成 ID 并运行
        pipeline_id = engine.pipeline_id
        session.register_pipeline(pipeline_id)
    """

    def __init__(self, session_dir: Path | str | None = None) -> None:
        self._session_dir = Path(session_dir) if session_dir else None

    def create(
        self,
        channel_type: str = "cli",
        channel_ref: str = "",
        session_id: str | None = None,
    ) -> SessionModel:
        """创建新会话（不生成 pipeline_id）。"""
        session = SessionModel(
            session_id=session_id or uuid.uuid4().hex[:12],
            channel_type=channel_type,
            channel_ref=channel_ref,
        )
        self._persist_session_state(session)
        logger.info("Session created: id=%s", session.session_id)
        return session

    async def create_or_restore(
        self,
        channel_type: str = "cli",
        channel_ref: str = "",
    ) -> SessionModel:
        """创建新会话或恢复已有 session_id。

        只恢复 session_id，对话历史由管道执行记录恢复。
        """
        saved = self._load_session_state()
        if saved:
            session = SessionModel(
                session_id=saved["session_id"],
                active_pipeline_id=saved.get("active_pipeline_id", ""),
                channel_type=channel_type,
                channel_ref=channel_ref,
            )
            self._persist_session_state(session)
            logger.info(
                "Session restored: id=%s, active_pipeline=%s",
                session.session_id,
                session.active_pipeline_id,
            )
            return session
        return self.create(channel_type=channel_type, channel_ref=channel_ref)

    def clear(self, session: SessionModel) -> None:
        """清空会话的管道引用列表。"""
        session.clear()
        self._persist_session_state(session)
        logger.info("Session cleared: id=%s", session.session_id)

    def _persist_session_state(self, session: SessionModel) -> None:
        """将会话状态持久化到文件。"""
        if self._session_dir is None:
            return
        self._session_dir.mkdir(parents=True, exist_ok=True)
        state_file = self._session_dir / ".current_session_state"
        try:
            state_file.write_text(
                json.dumps({"session_id": session.session_id, "active_pipeline_id": session.active_pipeline_id}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("Failed to persist session state: %s", exc)

    def _load_session_state(self) -> dict[str, str] | None:
        """从文件加载会话状态。"""
        if self._session_dir is None:
            return None
        state_file = self._session_dir / ".current_session_state"
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        id_file = self._session_dir / ".current_session_id"
        if id_file.exists():
            try:
                old_id = id_file.read_text(encoding="utf-8").strip()
                if old_id:
                    return {"session_id": old_id, "active_pipeline_id": ""}
            except OSError:
                pass
        return None
