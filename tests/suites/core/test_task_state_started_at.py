"""started_at 字段赋值与返回值契约测试。

背景：started_at 此前是死字段（仅有定义 types.py，无任何赋值点），
导致任务级耗时观测失效、僵尸任务无时间戳依据。
本测试覆盖 start_task / resume_task / reset_to_pending 对 started_at
的设/留/清语义，以及三者返回 task 的统一契约。
"""

from __future__ import annotations

import tempfile
from datetime import datetime

import pytest

from tasks.service import TaskService
from tasks.types import TaskStatus


def _make_service() -> TaskService:
    """创建使用临时目录的 TaskService 实例。"""
    tmp_dir = tempfile.mkdtemp(prefix="test_started_at_")
    return TaskService(data_dir=tmp_dir)


class TestStartedAtAssignment:
    """started_at 在状态转换中的设/留/清语义。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_start_task_sets_started_at(self) -> None:
        """start_task 后 started_at 应为可解析的 isoformat 时间戳。"""
        task = await self.svc.create_task(title="启动")
        await self.svc.start_task(task.id)
        fetched = self.svc.get_task(task.id)
        assert fetched.started_at is not None
        # 必须是合法 isoformat（非空串、非占位符）
        parsed = datetime.fromisoformat(fetched.started_at)
        assert parsed.year == 2026 or parsed.year >= 2020

    @pytest.mark.asyncio
    async def test_start_task_returns_task(self) -> None:
        """start_task 应返回 task 对象（与 resume_task 统一），不再是 None。"""
        task = await self.svc.create_task(title="启动返回值")
        result = await self.svc.start_task(task.id)
        assert result is not None
        assert result.id == task.id
        assert result.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_resume_task_preserves_existing_started_at(self) -> None:
        """resume_task 不应覆盖已有 started_at（暂停+恢复不能抹掉已运行时长）。"""
        task = await self.svc.create_task(title="暂停恢复")
        await self.svc.start_task(task.id)
        started_before = self.svc.get_task(task.id).started_at
        assert started_before is not None

        await self.svc.pause_task(task.id)
        await self.svc.resume_task(task.id)

        started_after = self.svc.get_task(task.id).started_at
        assert started_after == started_before, (
            "resume_task 不应覆盖已有 started_at"
        )

    @pytest.mark.asyncio
    async def test_resume_task_backfills_missing_started_at(self) -> None:
        """resume_task 对 started_at 缺失的任务应补设（兼容历史脏数据）。"""
        task = await self.svc.create_task(title="脏数据恢复")
        await self.svc.start_task(task.id)
        # 模拟历史脏数据：手动抹掉 started_at 但保持 stopped 状态
        fetched = self.svc.get_task(task.id)
        fetched.started_at = None
        fetched.status = TaskStatus.STOPPED
        await self.svc.save_task(fetched)

        await self.svc.resume_task(task.id)
        after = self.svc.get_task(task.id)
        assert after.started_at is not None, "resume 应为缺失的 started_at 补设"

    @pytest.mark.asyncio
    async def test_reset_to_pending_clears_started_at(self) -> None:
        """reset_to_pending 后 started_at 应为 None（回到未执行态）。"""
        task = await self.svc.create_task(title="重置")
        await self.svc.start_task(task.id)
        assert self.svc.get_task(task.id).started_at is not None

        await self.svc.reset_to_pending(task.id)
        after = self.svc.get_task(task.id)
        assert after.started_at is None, "reset_to_pending 应清空 started_at"

    @pytest.mark.asyncio
    async def test_reset_to_pending_returns_task(self) -> None:
        """reset_to_pending 正常路径应返回 task 对象。"""
        task = await self.svc.create_task(title="重置返回值")
        await self.svc.start_task(task.id)
        result = await self.svc.reset_to_pending(task.id)
        assert result is not None
        assert result.id == task.id
        assert result.status == TaskStatus.PENDING
