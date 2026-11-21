# @feature: FP-0.2.二 文件系统工具 | @ci: python-coverage
"""fs_tools.move_file 事件循环活性锁（合宿心跳不冻）。

事故根因（审计 A-9，2026-11-12）：插件 SDK ``_invoke_callable`` 只对**同步**
handler 做 ``asyncio.to_thread`` 下放，``move_file`` 是 ``async def``——overwrite
分支的 ``shutil.rmtree``（目标为大目录时十秒级）留在事件循环里，同函数尾部的
``shutil.move`` 却早已下放：同一个函数一半下一半不下，属漏改。

宿主 watchdog 判主循环心跳停滞 >30s 即整组 ``os._exit(1)``（``light`` 宿主
20 成员连坐）。

锁面：覆盖分支的删除与随后的移动合并进同一个线程内调用体——观测指标：
心跳 tick 间隔不出现 ≥0.1s 空窗（假身阻塞量级 0.3s，红绿区分度充足），
且两段操作落在同一个 worker 线程（一次跨线程，不拆两次）。

外部依赖（文件系统 IO 故障/耗时）按测试纪律替换为假身；位置闸、覆盖语义、
失败报文全真实。
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from agentos_builtin_tools import fs_tools
from agentos_builtin_tools.fs_tools import move_file

pytestmark = pytest.mark.unit

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
# 「事件循环未被饿死」的机械判据——假身阻塞量级 0.3s 必破此界。
_MAX_TICK_GAP = 0.1

_STUB_DELAY = 0.3


class _Ticker:
    """心跳协程：记录 tick 间隔 + 被测段收尾时刻。

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
        # 预热：让至少两 tick 落在被测调用之前（阻塞段前有基线 tick）
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


def _ws(tmp_path: Any) -> Any:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：in-loop 同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_STUB_DELAY)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：overwrite 覆盖分支的 rmtree + 移动（同一线程体）
# ============================================================


async def test_move_overwrite_keeps_event_loop_ticking(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖目标目录（假身 rmtree 阻塞 0.3s）期间心跳持续打点，覆盖语义不变，
    删除与移动落在同一 worker 线程。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")
    dst_dir = ws / "b"
    dst_dir.mkdir()
    (dst_dir / "stale.txt").write_text("stale", encoding="utf-8")

    loop_thread = threading.get_ident()
    rmtree_threads: list[int] = []
    move_threads: list[int] = []
    real_move = shutil.move
    real_rmtree = shutil.rmtree

    def _slow_rmtree(path: Any, **_kwargs: Any) -> None:
        rmtree_threads.append(threading.get_ident())
        time.sleep(_STUB_DELAY)
        real_rmtree(path)

    def _tracked_move(src: Any, dst: Any) -> Any:
        move_threads.append(threading.get_ident())
        return real_move(src, dst)

    monkeypatch.setattr(
        fs_tools, "shutil", SimpleNamespace(rmtree=_slow_rmtree, move=_tracked_move)
    )

    async with _Ticker() as ticker:
        result = await move_file("a.txt", "b", overwrite=True, workspace=str(ws))

    assert ticker.max_gap < _MAX_TICK_GAP
    assert len(rmtree_threads) == 1
    assert rmtree_threads[0] != loop_thread
    assert move_threads[0] != loop_thread
    # 一次跨线程跑完「删除 + 移动」（不拆成两次 to_thread）
    assert rmtree_threads[0] == move_threads[0]
    assert result.success is True, result.error
    assert (ws / "b").is_file()
    assert (ws / "b").read_text(encoding="utf-8") == "A"


async def test_move_overwrite_rmtree_error_is_clean_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖分支删除故障：仍归为 OSError → 「Move error」显式失败（语义不变）。"""
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("A", encoding="utf-8")
    (ws / "b").mkdir()

    def _boom_rmtree(_path: Any, **_kwargs: Any) -> None:
        raise OSError("rmtree bombed")

    monkeypatch.setattr(
        fs_tools, "shutil", SimpleNamespace(rmtree=_boom_rmtree, move=shutil.move)
    )

    result = await move_file("a.txt", "b", overwrite=True, workspace=str(ws))

    assert result.success is False
    assert "Move error" in (result.error or "")
    assert (ws / "a.txt").exists()
