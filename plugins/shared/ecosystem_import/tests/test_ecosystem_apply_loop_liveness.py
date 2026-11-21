# @feature: FP-0.2.外部生态导入 A批 | @ci: python-coverage
"""``ecosystem_apply`` 落地段事件循环活性锁（合宿心跳不冻）。

事故根因（2026-11-12 审计 A-8）：插件 SDK ``_invoke_callable`` 只对**同步**
handler 做 ``asyncio.to_thread`` 下放，async handler 直接在合宿宿主事件循环里
执行。``EcosystemApplyTool.execute`` 是 ``async def``，其 ``apply_plugin`` 落地段
是纯同步文件操作（``skill_landing._stage_and_commit`` 的整树 ``copytree`` /
覆盖分支 ``rmtree``，外加来源快照 git 子进程），源插件树体量大时十秒~分钟级；
阻塞 >30s 即宿主 watchdog 判心跳停滞、整组 ``os._exit(1)``（light 合宿连坐）。

锁面：``apply_plugin`` 整段出事件循环（观测指标=心跳 tick 间隔不出现 ≥0.1s
空窗；假身阻塞量级 0.3s，红绿区分度充足）。落地真值与错误语义不变——
``to_thread`` 原样透传返回值/``LandError``（工具边界照旧翻译成失败信封）。

隔离：源插件树与用户根都在 ``tmp_path``（真实文件系统，不 mock 内部实现）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import skill_landing
from codex_fixtures import load_tool_module

pytestmark = pytest.mark.unit

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
# 「事件循环未被饿死」的机械判据——假身阻塞量级 0.3s 必破此界。
_MAX_TICK_GAP = 0.1

# 假身阻塞量级（落地段的现实量级缩影）
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


def _slow_apply(monkeypatch: pytest.MonkeyPatch, delay: float) -> list[int]:
    """把 ``tool.apply_plugin`` 换成「等 delay 秒后跑真实落地」的假身。

    返回调用计数列表（断言落地确实发生过一次，而非被跳过）。
    """
    mod = load_tool_module()
    real = skill_landing.apply_plugin
    calls: list[int] = []

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        time.sleep(delay)
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "apply_plugin", _wrapped)
    return calls


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：协程内同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_BLOCK_SECS)
    assert ticker.max_gap >= 0.2


async def test_apply_landing_keeps_event_loop_ticking(
    mit_skills_plugin: Path, user_root_env: Path, apply_tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整树落地（0.3s 量级阻塞）期间心跳持续打点；落地真值与报告语义不变。"""
    calls = _slow_apply(monkeypatch, _BLOCK_SECS)

    async with _Ticker() as ticker:
        result = await apply_tool.execute({"source_dir": str(mit_skills_plugin)})

    assert ticker.max_gap < _MAX_TICK_GAP, (
        f"落地段冻结事件循环（期间心跳最大空窗 {ticker.max_gap:.3f}s）"
    )
    assert calls == [1]
    assert result.success is True
    assert sorted(s["skill_id"] for s in result.output["skills_written"]) == [
        "brainstorming",
        "writing-plans",
    ]
    assert (user_root_env / "skills" / "brainstorming" / "SKILL.md").is_file()
    assert (user_root_env / "ecosystem_imports.json").is_file()


async def test_force_overwrite_keeps_event_loop_ticking(
    mit_skills_plugin: Path, user_root_env: Path, apply_tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """force 覆盖分支（目标 ``rmtree`` + 换入）同样出事件循环；残留文件被真值同步清掉。"""
    first = await apply_tool.execute({"source_dir": str(mit_skills_plugin)})
    assert first.success is True
    stale = user_root_env / "skills" / "brainstorming" / "stale.md"
    stale.write_text("上一版残留\n", encoding="utf-8")
    calls = _slow_apply(monkeypatch, _BLOCK_SECS)

    async with _Ticker() as ticker:
        result = await apply_tool.execute(
            {"source_dir": str(mit_skills_plugin), "force": True}
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert calls == [1]
    assert result.success is True
    assert not stale.exists(), "force 覆盖未清残留（rmtree 换入语义退化）"


async def test_landing_error_crosses_thread_as_failure_envelope(
    mit_skills_plugin: Path, user_root_env: Path, apply_tool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """线程内异常语义不变：``LandError`` 照旧翻译成失败信封（码与原因透传）。"""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        time.sleep(_BLOCK_SECS)
        raise skill_landing.LandError("模拟落地失败", "LAND_FAILED")

    monkeypatch.setattr(load_tool_module(), "apply_plugin", _boom)

    async with _Ticker() as ticker:
        result = await apply_tool.execute({"source_dir": str(mit_skills_plugin)})

    assert ticker.max_gap < _MAX_TICK_GAP
    assert result.success is False
    assert result.error_code == "LAND_FAILED"
    assert "模拟落地失败" in result.error
    assert not (user_root_env / "ecosystem_imports.json").exists()
