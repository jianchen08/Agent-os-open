# @feature: FP-0.2.可观测性 | @ci: python-plugins-test
"""ProgressReporter 进度节流器测试（task_observability 任务 2）。

钉死语义：bash 工具执行中的 stdout 增量按阈值推送前端（tool_progress）——
- 未达阈值（< min_chars 且 距上次推送 < min_interval_s）不推
- 达到字节阈值（默认 ~1KB）即推
- 距上次推送超过时间阈值（默认 2s）时，下次 report 即推
- close() 冲刷残留缓冲
- 单次 delta 超限时保留尾部（防超大块刷屏）
- close 后继续 report 静默丢弃（后台 _read_output 任务尾巴）

时间用注入 clock 控制，测试不依赖真实 sleep。
"""

from __future__ import annotations

import pytest

from progress_reporter import ProgressReporter

pytestmark = pytest.mark.unit


class _FakeClock:
    """可控单调时钟。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_reporter(flushed: list, clock: _FakeClock, **kwargs) -> ProgressReporter:
    def _on_flush(delta: str, bytes_total: int) -> None:
        flushed.append((delta, bytes_total))

    defaults = {"min_chars": 1024, "min_interval_s": 2.0, "time_fn": clock}
    defaults.update(kwargs)
    return ProgressReporter(_on_flush, **defaults)


def test_below_threshold_no_flush() -> None:
    clock = _FakeClock()
    flushed: list = []
    reporter = _make_reporter(flushed, clock)

    reporter.report("short line\n" * 10)  # ~120 chars < 1024，且 < 2s

    assert flushed == []


def test_byte_threshold_triggers_flush() -> None:
    clock = _FakeClock()
    flushed: list = []
    reporter = _make_reporter(flushed, clock)

    reporter.report("x" * 600)
    reporter.report("y" * 600)  # 累计 1200 chars ≥ 1024 → 推

    assert len(flushed) == 1
    delta, bytes_total = flushed[0]
    assert "x" * 600 in delta
    assert "y" * 600 in delta
    assert bytes_total == 1200


def test_time_threshold_triggers_flush() -> None:
    clock = _FakeClock()
    flushed: list = []
    reporter = _make_reporter(flushed, clock)

    reporter.report("line\n")  # 起始 flush 时钟锚定
    clock.advance(2.5)  # 超过 2s
    reporter.report("late line\n")  # 下次 report 即推

    assert len(flushed) == 1
    delta, _ = flushed[0]
    assert "late line" in delta


def test_close_flushes_remainder() -> None:
    clock = _FakeClock()
    flushed: list = []
    reporter = _make_reporter(flushed, clock)

    reporter.report("tail without newline")
    reporter.close()

    assert len(flushed) == 1
    assert flushed[0][0] == "tail without newline"


def test_huge_delta_capped_tail_kept() -> None:
    clock = _FakeClock()
    flushed: list = []
    reporter = _make_reporter(flushed, clock)

    huge = "A" * 9000 + "END_MARKER"
    reporter.report(huge)  # 单块超限 → 保留尾部

    assert len(flushed) == 1
    delta, _ = flushed[0]
    assert len(delta) < 9000
    assert "END_MARKER" in delta  # 尾部保留


def test_report_after_close_silent() -> None:
    clock = _FakeClock()
    flushed: list = []
    reporter = _make_reporter(flushed, clock)

    reporter.close()
    reporter.report("x" * 5000)  # 后台任务尾巴 → 静默丢弃

    assert flushed == []


def test_empty_report_noop() -> None:
    clock = _FakeClock()
    flushed: list = []
    reporter = _make_reporter(flushed, clock)

    reporter.report("")

    assert flushed == []
