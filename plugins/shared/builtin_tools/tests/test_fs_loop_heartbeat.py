# @feature: FP-0.2.二 文件系统工具 | @ci: python-coverage
"""fs_tools 事件循环不空窗回归锁（async-def-with-sync-body 盲区根修）。

事故（2026-11-16 方案真源）：``_check_workspace_path``/``file_read`` 在
``async def`` 体内直接做 ``Path.resolve``/``exists`` + 真实 I/O，D 盘重载 +
并发下单次 58–213s，冻结合宿宿主心跳 → watchdog 停滞自杀。修复 = 阻塞点
``asyncio.to_thread`` 包裹 + workspace 根 resolve 幂等缓存。

断行为（不钉实现）：
- file_read 全链路执行期间事件循环持续打点（``Path.resolve`` 注入 2s 阻塞
  ——修前该阻塞落在事件循环线程，心跳停摆；修后在 worker 线程）；
- workspace 根 resolve 缓存：同 root_str 首次解析与缓存命中输出等价，
  第二次调用不再触发 ``Path.resolve``（幂等缓存语义）。

配方承接 test_fs_move_loop_liveness.py 的 _Ticker 心跳断言式（max_gap
观测，阻塞量级 2s 必破 0.1s 空窗界——红绿区分度充足）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from agentos_builtin_tools.fs_tools import (
    _WORKSPACE_ROOT_RESOLVE_CACHE,
    file_read,
)

pytestmark = pytest.mark.unit

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等）。0.1s 周期 ping
# 在 Windows 定时器下抖动 ~0.11s，取 0.5s 为「事件循环未被饿死」机械判据
# （>4x 抖动余量，假身阻塞 2s 必破——红绿区分度充足）。
_MAX_TICK_GAP = 0.5

# 假身阻塞量级（任务书口径 2s）：修前 resolve 在事件循环内必产生 ≥2s 空窗。
_RESOLVE_STUB_DELAY = 2.0


class _Ticker:
    """心跳协程：记录 tick 间隔 + 被测段收尾时刻（末段计入，停滞不漏判）。"""

    def __init__(self, interval: float = 0.1) -> None:
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


@pytest.fixture(autouse=True)
def _clear_root_cache() -> Any:
    """根解析缓存逐用例清空（模块级缓存的用例间隔离）。"""
    _WORKSPACE_ROOT_RESOLVE_CACHE.clear()
    yield
    _WORKSPACE_ROOT_RESOLVE_CACHE.clear()


def _install_slow_resolve(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Path.resolve 注入 2s 阻塞（真实调用链仍返回正确解析结果）。"""
    resolved_targets: list[Path] = []
    real_resolve = Path.resolve

    def _slow_resolve(self: Path, strict: bool = False) -> Path:
        resolved_targets.append(self)
        time.sleep(_RESOLVE_STUB_DELAY)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _slow_resolve)
    return resolved_targets


async def test_file_read_keeps_event_loop_ticking(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file_read 期间心跳持续打点（resolve 阻塞已出事件循环——盲区根修锁面）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("心跳不断", encoding="utf-8")
    _install_slow_resolve(monkeypatch)

    async with _Ticker() as ticker:
        result = await file_read("hello.txt", workspace=str(ws))

    assert result.success is True, result.error
    assert "心跳不断" in result.output["content"]
    assert ticker.max_gap < _MAX_TICK_GAP, (
        f"事件循环出现 {ticker.max_gap:.2f}s 空窗——resolve 阻塞未出事件循环"
    )


async def test_root_resolve_cache_hits_without_second_resolve(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 root 第二次调用走缓存：Path.resolve 不再被触发（幂等缓存语义）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("A", encoding="utf-8")

    resolve_calls = 0
    real_resolve = Path.resolve

    def _counting_resolve(self: Path, strict: bool = False) -> Path:
        nonlocal resolve_calls
        resolve_calls += 1
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _counting_resolve)

    first = await file_read("a.txt", workspace=str(ws))
    assert first.success is True, first.error
    first_count = resolve_calls
    assert first_count > 0, "首次调用必须发生根解析（缓存冷启动）"

    second = await file_read("a.txt", workspace=str(ws))
    assert second.success is True, second.error
    # 根解析恰发生一次：首次 = 1（根）+ K（目标/结果解析点），第二次增量 = K
    # （根走缓存）= 首次计数 - 1。若缓存失效，第二次增量会再多 1。
    assert resolve_calls - first_count == first_count - 1, (
        f"根缓存未生效：首次 {first_count} 次，二次调用后 {resolve_calls} 次"
        f"（增量 {resolve_calls - first_count} ≠ {first_count - 1}）"
    )
    # 缓存命中与首次解析等价：同 root_str 取同一 resolved 对象
    cached = _WORKSPACE_ROOT_RESOLVE_CACHE[str(ws)]
    assert str(cached) == str(ws), "缓存值与首次解析输出不一致"


async def test_root_resolve_cache_same_input_same_output(tmp_path: Any) -> None:
    """缓存正确性：同输入同输出（不同 root_str 各自解析，互不串值）。"""
    from agentos_builtin_tools.fs_tools import _resolve_workspace_root_cached

    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()

    r1a = _resolve_workspace_root_cached(str(ws1))
    r2 = _resolve_workspace_root_cached(str(ws2))
    r1b = _resolve_workspace_root_cached(str(ws1))

    assert r1a == r1b
    assert r1a != r2
    assert _WORKSPACE_ROOT_RESOLVE_CACHE[str(ws1)] == r1b
    assert _WORKSPACE_ROOT_RESOLVE_CACHE[str(ws2)] == r2
