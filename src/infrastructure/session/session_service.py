"""会话服务 — 管理会话（筐）的创建、恢复、清空。

会话只是一个筐，装 pipeline_run_id 列表。
对话历史、任务、执行记录都不归会话管。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from infrastructure.session.models import SessionModel

logger = logging.getLogger(__name__)


class SessionService:
    """会话服务 — 管理 session_id 的创建和持久化。

    Usage::

        svc = SessionService(session_dir=path)
        session = await svc.create_or_restore(channel_type="cli")
        pipeline_id = session_svc.prepare_run(session)
        result = await engine.run(pipeline_id=pipeline_id, ...)
    """

    def __init__(self, session_dir: Path | str | None = None) -> None:
        self._session_dir = Path(session_dir) if session_dir else None

    def create(
        self,
        channel_type: str = "cli",
        channel_ref: str = "",
        session_id: str | None = None,
    ) -> SessionModel:
        """创建新会话，立即生成 active_pipeline_id 并持久化。"""
        session = SessionModel(
            session_id=session_id or uuid.uuid4().hex[:12],
            channel_type=channel_type,
            channel_ref=channel_ref,
        )
        # 立即生成 pipeline_id，保证持久化后永远有值
        session.generate_pipeline_id()
        self._persist_session_state(session)
        logger.info(
            "Session created: id=%s, pipeline=%s",
            session.session_id, session.active_pipeline_id,
        )
        return session

    async def create_or_restore(
        self,
        channel_type: str = "cli",
        channel_ref: str = "",
    ) -> SessionModel:
        """创建新会话或恢复 session_id。

        只恢复 session_id，对话历史由 cli_main 通过检查点恢复。
        恢复后保证 active_pipeline_id 非空。
        """
        saved = self._load_session_state()
        if saved:
            session = SessionModel(
                session_id=saved["session_id"],
                active_pipeline_id=saved.get("active_pipeline_id", ""),
                channel_type=channel_type,
                channel_ref=channel_ref,
            )
            # 兜底：状态文件中 active_pipeline_id 为空时立即补生成
            if not session.active_pipeline_id:
                session.generate_pipeline_id()
                logger.info(
                    "Session restored with new pipeline (was empty): "
                    "id=%s, pipeline=%s",
                    session.session_id, session.active_pipeline_id,
                )
            self._persist_session_state(session)
            logger.info(
                "Session restored: id=%s, active_pipeline=%s",
                session.session_id, session.active_pipeline_id,
            )
            return session
        return self.create(channel_type=channel_type, channel_ref=channel_ref)

    def prepare_run(self, session: SessionModel) -> str:
        """为管道执行返回 pipeline_id。

        有 active_pipeline_id 就沿用，没有就生成新的。
        """
        if session.active_pipeline_id:
            session.touch()
            return session.active_pipeline_id
        pid = session.generate_pipeline_id()
        self._persist_session_state(session)
        return pid

    def clear(self, session: SessionModel) -> None:
        """清空会话：清管道列表，生成新 active_pipeline_id。"""
        session.clear()
        self._persist_session_state(session)
        logger.info("Session cleared: id=%s", session.session_id)

    # ── 会话状态持久化 ──────────────────────────────────

    def _persist_session_state(self, session: SessionModel) -> None:
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
        if self._session_dir is None:
            return None
        state_file = self._session_dir / ".current_session_state"
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        # 兼容旧格式
        id_file = self._session_dir / ".current_session_id"
        if id_file.exists():
            try:
                old_id = id_file.read_text(encoding="utf-8").strip()
                if old_id:
                    return {"session_id": old_id, "active_pipeline_id": ""}
            except OSError:
                pass
        return None
