# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""FileReverser 删除段事件循环活性锁（合宿心跳不冻）。

审计 A-8 同族（P2-1）：``async def _reverse_create`` 内的 ``shutil.rmtree`` 是同步
整树删除（目录保护允许删「本操作创建的目录」，其内容量级不受控）。回滚插件与
``task``/``isolation`` 等同宿 ``light`` 组，事件循环被阻塞超 watchdog 阈值即整组
``os._exit(1)``。锁面 = 删除段出事件循环（观测指标：心跳 tick 间隔不出现
≥0.1s 空窗；假身阻塞 0.3s，红绿区分度充足）；删除结果与目录保护语义不变。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
# 「事件循环未被饿死」的机械判据——假身阻塞量级 0.3s 必破此界。
_MAX_TICK_GAP = 0.1

_BLOCK_SECS = 0.3


class _Ticker:
    """心跳协程：记录 tick 间隔 + 被测段收尾时刻；事件循环被同步阻塞时
    max_gap（含末段 tick→收尾）超阈值。

    末段必须计入：阻塞期间停摆的心跳在收尾时无法补点（stop 已置位），
    只比 tick 与 tick 的间隔会把整段停滞漏判成 0。
    """

    def __init__(self, interval: float = 0.01) -> None:
        self._interval = interval
        self._ticks: list[float] = []
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._end: float = 0.0

    async def __aenter__(self) -> _Ticker:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(self._interval * 3)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        assert self._stop is not None
        assert self._task is not None
        self._end = time.monotonic()
        self._stop.set()
        await self._task

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            self._ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    @property
    def max_gap(self) -> float:
        points = [*self._ticks, self._end]
        return max(
            (b - a for a, b in zip(points, points[1:], strict=False)), default=0.0
        )


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：协程内同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_BLOCK_SECS)
    assert ticker.max_gap >= 0.2


async def test_reverse_create_rmtree_keeps_event_loop_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除本操作创建的目录（整树 rmtree 0.3s）期间心跳持续打点；
    返回语义不变（success + details.action=delete，目录确实消失）。"""
    import reversers
    from models import OperationLog, OperationType

    target = tmp_path / "created_dir"
    target.mkdir()
    (target / "payload.bin").write_bytes(b"x" * 1024)
    op = OperationLog(
        tool_name="create_directory",
        operation_type=OperationType.CREATE,
        target=str(target),
        before_state={"exists": False},  # 操作前不存在 = 本操作创建
    )
    real_rmtree = shutil.rmtree
    calls: list[str] = []

    def _slow_rmtree(path: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(str(path))
        time.sleep(_BLOCK_SECS)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(reversers.shutil, "rmtree", _slow_rmtree)

    async with _Ticker() as ticker:
        result = await reversers.FileReverser().reverse(op)

    assert ticker.max_gap < _MAX_TICK_GAP, (
        f"删除段冻结事件循环（期间心跳最大空窗 {ticker.max_gap:.3f}s）"
    )
    assert calls == [str(target)]
    assert result["success"] is True
    assert result["details"]["action"] == "delete"
    assert not target.exists()


async def test_reverse_create_rmtree_failure_reports_offloop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除失败语义不变：异常照旧被 ``reverse`` 兜底成失败结果，且阻塞段
    同样出事件循环（错误是值，不抛给管道）。"""
    import reversers
    from models import OperationLog, OperationType

    target = tmp_path / "created_dir"
    target.mkdir()
    (target / "f.txt").write_text("x", encoding="utf-8")
    op = OperationLog(
        tool_name="create_directory",
        operation_type=OperationType.CREATE,
        target=str(target),
        before_state={"exists": False},
    )

    def _refusing_rmtree(path: Any, *args: Any, **kwargs: Any) -> Any:
        time.sleep(_BLOCK_SECS)
        raise PermissionError("mock: 目录被占用")

    monkeypatch.setattr(reversers.shutil, "rmtree", _refusing_rmtree)

    async with _Ticker() as ticker:
        result = await reversers.FileReverser().reverse(op)

    assert ticker.max_gap < _MAX_TICK_GAP
    assert result["success"] is False
    assert "mock: 目录被占用" in result["message"]
    assert target.exists(), "删除失败后目录必须原样保留（不半删）"
