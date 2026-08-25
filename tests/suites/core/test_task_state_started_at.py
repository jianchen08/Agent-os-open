"""started_at 字段赋值与返回值契约测试。

契约：started_at 是任务级耗时观测与僵尸任务判定的时间戳依据，
start_task / resume_task / reset_to_pending 对 started_at 有设/留/清语义，
三者返回 task 保持统一契约。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

# 收集期路径自持：pytest 收集 tests/plugins/ 等包内测试目录时会把它 prepend
# 到 sys.path[0] 且驻留（如 tests/plugins/system/tasks/、human 的 service.py
# 同名劫持），本文件模块级 `from service import TaskService` 必须解析到
# plugins/shared/system/tasks/——无条件前置本插件目录（先去重）。
_TASKS_DIR = Path(__file__).resolve().parents[3] / "plugins" / "shared" / "system" / "tasks"
_SHARED_DIR = _TASKS_DIR.parent.parent
for _d in (_SHARED_DIR, _TASKS_DIR):
    _s = str(_d)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
sys.modules.pop("service", None)
sys.modules.pop("storage", None)

import task_types as _task_types_module  # noqa: E402
from service import TaskService  # noqa: E402
from task_types import TaskStatus  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_task_types():
    """固定 task_types 槽位为收集期绑定的实例。

    tests/plugins 等套件的 fixture 运行期会 pop/重导 task_types（制造新代际），
    使 `_task_crud`/`_task_state` 内懒加载的 TaskStatus 与测试模块绑定的
    枚举实例分叉（== 比较失败）。本 fixture 在每个用例前把本文件收集期
    绑定的版本写回 sys.modules，保证执行链内枚举同源。
    """
    sys.modules["task_types"] = _task_types_module
    yield


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
