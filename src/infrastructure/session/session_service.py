"""会话服务 — 统一管理会话生命周期。

提供会话创建、恢复、清空、持久化等功能，供 CLI 和 Web 通道共用。
替代 cli_main.py 中散落的会话管理逻辑。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from infrastructure.session.models import SessionModel

logger = logging.getLogger(__name__)

MAX_SESSION_MESSAGES = 100


class SessionService:
    """业务会话生命周期服务。

    协调 SessionModel（内存状态）与 PipelineCheckpointManager（持久化）。
    CLI 和 Web 通道共用同一个接口。

    Usage (CLI)::

        svc = SessionService(checkpoint_manager=cp_mgr, session_dir=path)
        session = await svc.create_or_restore(channel_type="cli")
        pipeline_id = session.generate_pipeline_id()
        result = await engine.run(pipeline_id=pipeline_id, ...)
        svc.update_after_run(session, result)
        await svc.save_on_exit(session)

    Usage (Web)::

        svc = SessionService(checkpoint_manager=cp_mgr)
        session = svc.create(channel_type="web", channel_ref=thread_id)
    """

    def __init__(
        self,
        checkpoint_manager: Any | None = None,
        session_dir: Path | str | None = None,
    ) -> None:
        self._checkpoint_manager = checkpoint_manager
        self._session_dir = Path(session_dir) if session_dir else None

    # ── 创建与恢复 ─────────────────────────────────────

    def create(
        self,
        channel_type: str = "cli",
        channel_ref: str = "",
        session_id: str | None = None,
    ) -> SessionModel:
        """创建新会话。"""
        session = SessionModel(
            session_id=session_id or uuid.uuid4().hex[:12],
            channel_type=channel_type,
            channel_ref=channel_ref,
        )
        self._persist_session_id(session.session_id)
        logger.info(
            "Session created: id=%s, channel=%s",
            session.session_id,
            channel_type,
        )
        return session

    async def create_or_restore(
        self,
        channel_type: str = "cli",
        channel_ref: str = "",
    ) -> SessionModel:
        """创建新会话或从检查点恢复。

        恢复优先级：
        1. 从磁盘加载 session_id → 查找对应检查点
        2. 回退：全局最新 session_end 检查点
        3. 都没有：创建新会话
        """
        saved_id = self._load_session_id()

        # 尝试 1：用保存的 session_id 查找检查点
        if saved_id and self._checkpoint_manager:
            try:
                data = await self._checkpoint_manager.get_latest(saved_id)
                if data:
                    return self._restore_from_checkpoint(
                        data, saved_id, channel_type, channel_ref,
                    )
            except Exception as exc:
                logger.debug("Restore from saved session_id failed: %s", exc)

        # 尝试 2：回退到全局最新 session_end 检查点
        if self._checkpoint_manager:
            try:
                data = await self._checkpoint_manager.get_latest_any(
                    phase="session_end",
                )
                if data is None:
                    data = await self._checkpoint_manager.get_latest_any()
                if data:
                    pid = data.get("metadata", {}).get("pipeline_id", "")
                    return self._restore_from_checkpoint(
                        data, pid or saved_id or "", channel_type, channel_ref,
                    )
            except Exception as exc:
                logger.debug("Fallback restoration failed: %s", exc)

        # 尝试 3：全新会话
        return self.create(channel_type=channel_type, channel_ref=channel_ref)

    def _restore_from_checkpoint(
        self,
        checkpoint_data: dict[str, Any],
        session_id: str,
        channel_type: str,
        channel_ref: str,
    ) -> SessionModel:
        """从检查点数据构建 SessionModel。"""
        messages = checkpoint_data.get("state", {}).get("messages", [])
        if not isinstance(messages, list):
            messages = []

        session = SessionModel(
            session_id=session_id or uuid.uuid4().hex[:12],
            channel_type=channel_type,
            channel_ref=channel_ref,
            conversation_history=messages[-MAX_SESSION_MESSAGES:],
        )
        self._persist_session_id(session.session_id)
        logger.info(
            "Session restored: id=%s, messages=%d",
            session.session_id,
            len(session.conversation_history),
        )
        return session

    # ── 每轮操作 ───────────────────────────────────────

    def prepare_run(self, session: SessionModel) -> str:
        """为新一轮管道执行准备 pipeline_id。

        在调用 engine.run() 之前调用。
        """
        return session.generate_pipeline_id()

    def update_after_run(
        self,
        session: SessionModel,
        final_state: dict[str, Any],
        initial_user_input: str = "",
    ) -> None:
        """管道执行完成后更新会话状态。"""
        final_messages = final_state.get("messages", [])
        if final_messages:
            session.conversation_history = list(final_messages)
        elif initial_user_input:
            session.conversation_history.append(
                {"role": "user", "content": initial_user_input},
            )
            raw_result = final_state.get("raw_result", "")
            if raw_result:
                session.conversation_history.append(
                    {"role": "assistant", "content": raw_result},
                )

        session.turn_count += 1
        if len(session.conversation_history) > MAX_SESSION_MESSAGES:
            session.conversation_history = (
                session.conversation_history[-MAX_SESSION_MESSAGES:]
            )
        session.touch()

    # ── 清空 ───────────────────────────────────────────

    async def clear(self, session: SessionModel) -> None:
        """处理 /clear：清空历史，保留 session_id。

        旧的 active_pipeline_id 保留，确保挂起的引擎和绑定的任务
        仍能通过 pipeline_id 找到正确的引擎实例。
        """
        session.clear_history()

        old_pipeline_id = session.active_pipeline_id
        if old_pipeline_id and self._checkpoint_manager:
            try:
                await self._checkpoint_manager.cleanup_old(
                    old_pipeline_id, keep_count=0,
                )
            except Exception as exc:
                logger.debug("Checkpoint cleanup on clear failed: %s", exc)

        logger.info(
            "Session cleared: session_id=%s (history reset, id preserved)",
            session.session_id,
        )

    # ── 持久化 ─────────────────────────────────────────

    async def save_on_exit(self, session: SessionModel) -> None:
        """退出时保存会话状态为检查点。

        使用 session_id 作为 key，phase="session_end"，
        以便下次启动时 create_or_restore 可以找到。
        """
        if not session.conversation_history or not self._checkpoint_manager:
            return
        try:
            await self._checkpoint_manager.save(
                session.session_id,
                {"messages": session.conversation_history},
                phase="session_end",
            )
        except Exception as exc:
            logger.debug("Save on exit failed: %s", exc)

    async def auto_checkpoint(self, session: SessionModel) -> None:
        """保存自动检查点（每轮执行后可选调用）。"""
        if not self._checkpoint_manager or not session.active_pipeline_id:
            return
        try:
            await self._checkpoint_manager.save(
                session.active_pipeline_id,
                {"messages": session.conversation_history},
                phase="auto",
            )
        except Exception as exc:
            logger.debug("Auto-checkpoint failed: %s", exc)

    # ── Session ID 持久化（CLI 专用）───────────────────

    def _persist_session_id(self, session_id: str) -> None:
        """将 session_id 写入磁盘。"""
        if self._session_dir is None:
            return
        self._session_dir.mkdir(parents=True, exist_ok=True)
        id_file = self._session_dir / ".current_session_id"
        try:
            id_file.write_text(session_id, encoding="utf-8")
        except OSError as exc:
            logger.debug("Failed to persist session_id: %s", exc)

    def _load_session_id(self) -> str | None:
        """从磁盘读取之前持久化的 session_id。"""
        if self._session_dir is None:
            return None
        id_file = self._session_dir / ".current_session_id"
        if id_file.exists():
            try:
                return id_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        return None
