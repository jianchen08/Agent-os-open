# @feature: FP-0.2.多端同步 T6 手动备份 | @ci: python-coverage
"""sync_agent 备份链事件循环活性锁（合宿心跳不冻）。

事故根因（2026-11-12 异步阻塞审计 A-3）：``run_backup`` 是 async handler，
其纯同步体（整工作空间 ``rmtree``/``copytree``/``rglob`` + ``_commit_path``
多条 ``git_cli.run_git``，单条 timeout 120s）直接在合宿宿主事件循环里跑。
宿主 watchdog 判心跳停滞 >30s 即整组 ``os._exit(1)``（light_stable 含 task/
tasks/isolation，备份一次卡住带走任务管理面）。

锁面（断行为）：
- 工作空间整树复制出事件循环——观测指标：心跳 tick 间隔不出现 ≥0.1s 空窗
  （假身阻塞 0.3s，红绿区分度充足）；
- 备份内全部 git 子进程出事件循环（每调 0.12s，≥5 调）；
- ``asyncio.Lock`` 留在协程侧：锁已被占用时备份不进入 commit 段，释放后照常完成
  （锁若被搬进线程则本用例不成立）。

外部依赖仅替换阻塞量级（真实 copytree/git 语义保留），内核读面按外部边界注入
fake 端口。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import backup
import pytest
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker
from backup import BackupConfig, BackupService
from git_cli import run_git as _real_run_git

pytestmark = pytest.mark.unit


class _FakeKernelReads:
    """内核读端口替身（db-admin / service-registry 是插件外边界）。"""

    async def list_table_rows(self, table: str) -> list[dict[str, Any]]:
        del table
        return []

    async def list_pipeline_messages(self, pipeline_id: str) -> list[dict[str, Any]]:
        del pipeline_id
        return []


def _service(tmp_path: Path) -> BackupService:
    return BackupService(
        BackupConfig(repo_root=tmp_path / "backup"), reads=_FakeKernelReads()
    )


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "nested").mkdir(parents=True)
    (ws / "nested" / "a.txt").write_text("payload", encoding="utf-8")
    return ws


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：备份纯同步体出事件循环
# ============================================================


async def test_workspace_copy_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整工作空间 copytree（0.3s）期间心跳持续打点，备份语义照常生效。"""
    ws = _workspace(tmp_path)
    service = _service(tmp_path)
    real_copytree = shutil.copytree

    def _slow_copytree(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", _slow_copytree)

    async with LoopTicker() as ticker:
        result = await service.run_backup([("ws1", str(ws))])

    assert ticker.max_gap < MAX_TICK_GAP
    assert result["ok"] is True
    assert result["workspace"]["committed"] is True
    assert result["workspace"]["files"] == 1
    # 副本内容真实落盘（copytree 不是空转）
    assert (service.staging / "workspace" / "ws1" / "nested" / "a.txt").is_file()


async def test_backup_git_calls_keep_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """备份内 git 子进程（add/status/commit/rev-parse，逐调 0.12s）出事件循环；
    真实 git 仓库与提交结果不变（只加延迟，不换实现）。"""
    ws = _workspace(tmp_path)
    service = _service(tmp_path)
    calls: list[str] = []

    def _slow_run_git(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(args[3] if len(args) > 3 else args))
        time.sleep(0.12)
        return _real_run_git(*args, **kwargs)

    monkeypatch.setattr(backup, "run_git", _slow_run_git)

    async with LoopTicker() as ticker:
        result = await service.run_backup([("ws1", str(ws))])

    assert ticker.max_gap < MAX_TICK_GAP
    assert len(calls) >= 5, f"git 调用面不足（{calls}）"
    assert result["workspace"]["committed"] is True


async def test_backup_waits_on_coroutine_side_lock(tmp_path: Path) -> None:
    """锁语义不变：``self._lock`` 是协程侧 asyncio.Lock——被占用时 commit 段
    不进入，释放后备份完成（锁若被搬进线程，本例不再成立）。"""
    ws = _workspace(tmp_path)
    service = _service(tmp_path)

    async with service._lock:
        task = asyncio.create_task(service.run_backup([("ws1", str(ws))]))
        await asyncio.sleep(0.05)
        assert not task.done(), "锁被占用时 commit 段不得进入"
    result = await task

    assert result["ok"] is True
    assert result["workspace"]["committed"] is True


async def test_restore_checkout_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恢复（检出 git read-tree/checkout-index + 全树计数）同链同步体，出事件循环；
    检出内容照常落地。"""
    ws = _workspace(tmp_path)
    service = _service(tmp_path)
    await service.run_backup([("ws1", str(ws))])
    dest = tmp_path / "restored"

    calls: list[str] = []

    def _slow_run_git(*args: Any, **kwargs: Any) -> Any:
        calls.append("git")
        time.sleep(0.12)
        return _real_run_git(*args, **kwargs)

    monkeypatch.setattr(backup, "run_git", _slow_run_git)

    async with LoopTicker() as ticker:
        result = await service.restore(part="workspace", commit=None, dest=dest, name="ws1")

    assert ticker.max_gap < MAX_TICK_GAP
    assert len(calls) >= 4
    assert result["files"] == 1
    assert (dest / "nested" / "a.txt").read_text(encoding="utf-8") == "payload"


async def test_concurrent_backups_serialize_on_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同实例并发触发：协程侧锁串行 commit 段（两次都成功，区间不交叠）。"""
    ws = _workspace(tmp_path)
    service = _service(tmp_path)
    real_commit = service._commit_workspace_part
    spans: list[tuple[float, float]] = []

    def _timed_commit(specs: Any) -> Any:
        started = time.monotonic()
        try:
            time.sleep(0.15)
            return real_commit(specs)
        finally:
            spans.append((started, time.monotonic()))

    monkeypatch.setattr(service, "_commit_workspace_part", _timed_commit)

    results = await asyncio.gather(
        service.run_backup([("ws1", str(ws))]),
        service.run_backup([("ws1", str(ws))]),
    )

    assert [r["ok"] for r in results] == [True, True]
    assert len(spans) == 2
    spans.sort()
    assert spans[0][1] <= spans[1][0], "commit 段交叠 = 协程侧锁未生效"
