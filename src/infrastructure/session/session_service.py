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
        task_service: Any | None = None,
        exec_storage: Any | None = None,
    ) -> None:
        self._checkpoint_manager = checkpoint_manager
        self._session_dir = Path(session_dir) if session_dir else None
        self._task_service = task_service
        self._exec_storage = exec_storage

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

        # 恢复旧的 pipeline_id 以保持任务绑定不断
        restored_pipeline_id = (
            checkpoint_data.get("metadata", {}).get("pipeline_id", "")
        )

        session = SessionModel(
            session_id=session_id or uuid.uuid4().hex[:12],
            channel_type=channel_type,
            channel_ref=channel_ref,
            conversation_history=messages[-MAX_SESSION_MESSAGES:],
            active_pipeline_id=restored_pipeline_id,
        )
        self._persist_session_id(session.session_id)
        logger.info(
            "Session restored: id=%s, pipeline=%s, messages=%d",
            session.session_id,
            restored_pipeline_id,
            len(session.conversation_history),
        )
        return session

    # ── 每轮操作 ───────────────────────────────────────

    def prepare_run(self, session: SessionModel) -> str:
        """为新一轮管道执行准备 pipeline_id。

        恢复后第一次 run 沿用旧 pipeline_id（保持任务绑定不断），
        之后每次 run 生成新 ID。
        """
        if session.active_pipeline_id:
            # 有旧 pipeline_id，继续沿用，不换
            session.touch()
            return session.active_pipeline_id
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
        """处理 /clear：清空历史，迁移任务到新管道，清理旧记录。

        流程：
        1. 生成新 pipeline_id
        2. 将现有任务的 pipeline_run_id 迁移到新 ID
        3. 删除旧主管道的执行记录
        4. 清空对话历史
        """
        old_pipeline_id = session.active_pipeline_id

        # 1. 生成新 pipeline_id
        new_pipeline_id = session.generate_pipeline_id()

        # 2. 迁移现有任务到新管道
        if old_pipeline_id and self._task_service:
            self._migrate_tasks(
                old_pipeline_id, new_pipeline_id,
            )

        # 3. 删除旧主管道的执行记录
        if old_pipeline_id and self._exec_storage:
            try:
                deleted = self._exec_storage.delete_by_session(
                    old_pipeline_id,
                )
                if deleted:
                    logger.info(
                        "Deleted %d execution records for old pipeline %s",
                        deleted,
                        old_pipeline_id,
                    )
            except Exception as exc:
                logger.debug(
                    "Failed to delete old pipeline records: %s", exc,
                )

        # 4. 清理旧管道的检查点
        if old_pipeline_id and self._checkpoint_manager:
            try:
                await self._checkpoint_manager.cleanup_old(
                    old_pipeline_id, keep_count=0,
                )
            except Exception as exc:
                logger.debug(
                    "Checkpoint cleanup on clear failed: %s", exc,
                )

        # 5. 清空对话历史
        session.clear_history()

        logger.info(
            "Session cleared: session_id=%s, pipeline %s → %s, "
            "history reset",
            session.session_id,
            old_pipeline_id,
            new_pipeline_id,
        )

    def _migrate_tasks(
        self,
        old_pipeline_id: str,
        new_pipeline_id: str,
    ) -> None:
        """将旧管道绑定的任务迁移到新管道。"""
        try:
            from tasks.types import TaskStatus

            migrated = 0
            for status in TaskStatus:
                tasks = self._task_service.list_by_status(status)
                for task in tasks:
                    if getattr(task, "pipeline_run_id", None) == old_pipeline_id:
                        self._task_service.bind_pipeline_run(
                            task.id, new_pipeline_id,
                        )
                        migrated += 1
                    if getattr(task, "parent_pipeline_id", None) == old_pipeline_id:
                        task.parent_pipeline_id = new_pipeline_id
                        if hasattr(self._task_service, "_storage"):
                            self._task_service._storage.save(task)
                        migrated += 1

            # 重新注册管道映射
            if migrated > 0 and self._exec_storage:
                root_id = None
                for status in TaskStatus:
                    for task in self._task_service.list_by_status(status):
                        if getattr(task, "pipeline_run_id", None) == new_pipeline_id:
                            root_id = self._task_service.get_root_task_id(task.id)
                            break
                    if root_id:
                        break
                if root_id:
                    self._exec_storage.register_pipeline(
                        new_pipeline_id, root_id,
                    )

            if migrated:
                logger.info(
                    "Migrated %d task references: %s → %s",
                    migrated,
                    old_pipeline_id,
                    new_pipeline_id,
                )
        except Exception as exc:
            logger.warning("Task migration failed: %s", exc)

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
