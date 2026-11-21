# @feature: FP-0.2.二 eval_harness 覆盖率收口 | @ci: python-coverage
"""mode_evolution async handler 事件循环活性锁（合宿心跳不冻）。

事故根因（2026-11-12）：插件 SDK ``_invoke_callable`` 只对**同步** handler 做
``asyncio.to_thread`` 下放，async handler 直接在合宿宿主事件循环里执行。
本插件两处 git 子进程调用是同步 ``subprocess.run``（``_git`` timeout=120s）：

- ``proposal(action='apply')`` 连跑三段（add / commit / rev-parse）——大仓索引
  锁等待、钩子脚本、慢盘都落在这条路径；
- ``eval_summarize`` 的 ``_git_head``（重放键基线）每轮一次。

事件循环被饿死 → 宿主 watchdog 判心跳停滞 >30s 即整组 ``os._exit(1)``
（2026-11-12 装机内核因此异常重启）。锁面：这两处出事件循环（观测指标=心跳
tick 间隔不出现 ≥0.1s 空窗；假身阻塞量级 0.25-0.3s，红绿区分度充足），
返回值/异常/超时语义不变（``to_thread`` 原样透传）。

隔离：账本目录与项目根都指到 tmp_path，提案链路真实落盘（git 子进程按测试纪律
替换为计时假身——被测算的是「阻塞调用是否出循环」，不是 git 本身）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(__file__))

_LEGAL_TARGET = "config/rules/liveness_gap_rule.md"

# 心跳观测阈值：宿主 watchdog 判停滞 30s 太宽（单测不可等），取 0.1s 作
# 「事件循环未被饿死」的机械判据——假身阻塞量级 0.25-0.3s 必破此界。
_MAX_TICK_GAP = 0.1


def _load_server_module() -> Any:
    """按唯一模块名装载 server.py（避开车道内裸名 server 的跨插件互覆）。"""
    path = os.path.join(os.path.dirname(__file__), "server.py")
    spec = importlib.util.spec_from_file_location(
        "eval_harness_service_server_loop_liveness", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server_env(tmp_path: Any, monkeypatch: Any) -> Any:
    """把账本目录与项目根都指到 tmp：真实落盘但不污染主仓。"""
    server = _load_server_module()
    reports = tmp_path / "reports" / "eval"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(server, "_EVAL_REPORTS", str(reports))
    monkeypatch.setattr(server, "_PROJECT_ROOT", str(project))
    return server


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


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：handler 内同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁一：proposal apply 的 git 子进程段不冻事件循环
# ============================================================


async def test_proposal_apply_git_subprocess_keeps_event_loop_ticking(
    server_env: Any, monkeypatch: Any
) -> None:
    """apply 三段 git（add/commit/rev-parse，各 0.25s）期间心跳持续打点；
    返回语义不变（commit 取 rev-parse 短哈希）。"""
    submitted = await server_env.proposal(
        action="submit", layer="L3", target=_LEGAL_TARGET,
        change_type="new_plugin", content="规则正文\n", motivation="m",
    )
    assert submitted["accepted"] is True
    seen: list[list[str]] = []

    def _slow_git(args: list[str]) -> tuple[int, str]:
        seen.append(list(args))
        time.sleep(0.25)
        return (0, "abc1234\n") if args and args[0] == "rev-parse" else (0, "")

    monkeypatch.setattr(server_env, "_git", _slow_git)

    async with _Ticker() as ticker:
        applied = await server_env.proposal(
            action="apply", proposal_id=submitted["proposal_id"]
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert applied["success"] is True
    assert applied["applied"] is True
    assert applied["commit"] == "abc1234"
    assert [args[0] for args in seen] == ["add", "commit", "rev-parse"]


async def test_proposal_apply_git_failure_reports_error_offloop(
    server_env: Any, monkeypatch: Any
) -> None:
    """git 失败语义不变：错误值如实回显（不抛异常、不谎报 applied），
    且阻塞段同样出事件循环。"""
    submitted = await server_env.proposal(
        action="submit", layer="L3", target=_LEGAL_TARGET,
        change_type="new_plugin", content="x", motivation="m",
    )

    def _refusing_git(args: list[str]) -> tuple[int, str]:
        time.sleep(0.25)
        return (128, "mock: refused")

    monkeypatch.setattr(server_env, "_git", _refusing_git)

    async with _Ticker() as ticker:
        applied = await server_env.proposal(
            action="apply", proposal_id=submitted["proposal_id"]
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert applied["success"] is False
    assert "git add 失败" in applied["error"]
    stored = server_env._load_ledger()["proposals"][submitted["proposal_id"]]
    assert stored["outcome"] == "submitted"


# ============================================================
# 主锁二：eval_summarize 的重放键基线取数不冻事件循环
# ============================================================


async def test_eval_summarize_git_head_keeps_event_loop_ticking(
    server_env: Any, monkeypatch: Any
) -> None:
    """_git_head（重放键 material_ref）0.3s 阻塞期间心跳持续打点，
    且基线哈希如实落账。"""

    def _slow_git_head() -> str:
        time.sleep(0.3)
        return "abc1234"

    monkeypatch.setattr(server_env, "_git_head", _slow_git_head)

    async with _Ticker() as ticker:
        out = await server_env.eval_summarize(
            "coding",
            [{"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}],
        )

    assert ticker.max_gap < _MAX_TICK_GAP
    assert out["success"] is True
    assert out["passed"] == 1
    round_row = server_env._load_ledger()["rounds"][-1]
    assert round_row["material_ref"] == "abc1234"
