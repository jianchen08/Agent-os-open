# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""completed 进程即时清理 + 命令日志保留上限测试。

_on_output_task_done 在进程输出读完（=进程已退出）时，应立即从 active_processes
清理内存记录；日志按退出形态分级保留：正常退出（rc==0）入宽限删除队列
（宽限窗覆盖 execute 返回后 summary/read_log 的磁盘降级读取——竞态契约见
test_summary_race），过窗由看门狗 tick flush 删除；非正常退出保留便于排障；
历史残留由启动清扫兜底（>7 天 bash_*.log，带数量汇总）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import pytest
from bash_types import ProcessInfo
from process_manager import ProcessManager

pytestmark = pytest.mark.unit


class _DoneTask:
    """模拟 asyncio.Task，已正常完成，result() 返回 None。"""

    def result(self):
        return None


@pytest.fixture
def pm(tmp_path):
    return ProcessManager(log_dir=tmp_path / "logs")


def _make_info(pid: int, status: str, log_file: Path, exit_code: int | None = 0) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        command="some cmd",
        start_time=0,
        log_file=log_file,
        status=status,
        exit_code=exit_code,
    )


def test_on_output_task_done_clears_completed(pm, tmp_path):
    """status=completed 时 _on_output_task_done 应清理内存记录。"""
    log_file = tmp_path / "bash_100.log"
    pm.active_processes[100] = _make_info(100, "completed", log_file)

    pm._on_output_task_done(100, _DoneTask())

    assert 100 not in pm.active_processes, "completed 进程应被清理"


def test_on_output_task_done_clears_error(pm, tmp_path):
    """status=error 时也应清理。"""
    log_file = tmp_path / "bash_101.log"
    pm.active_processes[101] = _make_info(101, "error", log_file, exit_code=1)

    pm._on_output_task_done(101, _DoneTask())

    assert 101 not in pm.active_processes


def test_on_output_task_done_clears_terminated(pm, tmp_path):
    """status=terminated 时也应清理。"""
    log_file = tmp_path / "bash_102.log"
    pm.active_processes[102] = _make_info(102, "terminated", log_file)

    pm._on_output_task_done(102, _DoneTask())

    assert 102 not in pm.active_processes


def test_on_output_task_done_keeps_running(pm, tmp_path):
    """status=running 时不应清理（防御性，靠看门狗兜底）。"""
    log_file = tmp_path / "bash_103.log"
    pm.active_processes[103] = _make_info(103, "running", log_file)

    pm._on_output_task_done(103, _DoneTask())

    assert 103 in pm.active_processes, "running 进程不应被清理"


def test_on_output_task_done_unknown_pid_no_error(pm):
    """pid 不在 active_processes 时不应报错（幂等）。"""
    pm._on_output_task_done(99999, _DoneTask())
    # 未知 pid 不应被登记，注册表保持原状
    assert 99999 not in pm.active_processes
    assert pm.get_process_info(99999) is None


def test_cleared_pid_get_process_info_returns_none(pm, tmp_path):
    """completed 清理后，get_process_info 应返回 None。"""
    log_file = tmp_path / "bash_104.log"
    pm.active_processes[104] = _make_info(104, "completed", log_file)

    pm._on_output_task_done(104, _DoneTask())

    assert pm.get_process_info(104) is None


def test_cleared_pid_get_summary_returns_none(pm, tmp_path):
    """completed 清理后，get_summary 应返回 None。"""
    log_file = tmp_path / "bash_105.log"
    log_file.write_text("dummy", encoding="utf-8")
    pm.active_processes[105] = _make_info(105, "completed", log_file)

    pm._on_output_task_done(105, _DoneTask())

    assert pm.get_summary(105) is None


# ── 日志保留上限：正常退出宽限后删，非正常退出保留 ────────────────


def test_completed_log_deleted_after_grace(tmp_path):
    """rc==0（completed）：宽限窗内日志保留（summary/read_log 磁盘降级靠它），
    过窗后 flush 删除。"""
    pm = ProcessManager(log_dir=tmp_path / "logs", log_delete_grace_seconds=0.0)
    log_file = pm.log_dir / "bash_200.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("# Bash Command Log\n# Process ended with exit code: 0\noutput", encoding="utf-8")
    pm.active_processes[200] = _make_info(200, "completed", log_file, exit_code=0)

    pm._on_output_task_done(200, _DoneTask())

    # 默认宽限（300s）下不删：磁盘降级路径仍可读
    assert log_file.exists(), "宽限窗内日志必须保留（get_summary/read_log 降级真理源）"
    assert pm.read_log_by_pid(200) is not None

    # 宽限归零 + flush（看门狗 tick 同款入口）→ 删除
    pm.flush_expired_log_deletes()
    assert not log_file.exists(), "宽限过窗后正常退出日志应被删除"
    assert pm.read_log_by_pid(200) is None

    # flush 幂等：队列为空后重复调用不报错
    pm.flush_expired_log_deletes()


def test_completed_log_within_default_grace_kept_after_flush(pm, tmp_path):
    """默认宽限（300s）内 flush 不删（时间未到，非队首逐出误删）。"""
    log_file = pm.log_dir / "bash_210.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("x", encoding="utf-8")
    pm.active_processes[210] = _make_info(210, "completed", log_file, exit_code=0)

    pm._on_output_task_done(210, _DoneTask())
    pm.flush_expired_log_deletes()

    assert log_file.exists(), "宽限窗内 flush 不得删除"
    assert len(pm._logs_pending_delete) == 1  # noqa: SLF001 — 队列未清空的可观察态


def test_error_and_terminated_logs_kept_even_after_flush(pm, tmp_path):
    """非正常退出（error/terminated）：日志永久保留便于排障，flush 不触碰
    （≥2 组区分输入）。"""
    for pid, status in ((201, "error"), (202, "terminated")):
        log_file = pm.log_dir / f"bash_{pid}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("stack trace", encoding="utf-8")
        pm.active_processes[pid] = _make_info(pid, status, log_file, exit_code=1)

        pm._on_output_task_done(pid, _DoneTask())
        pm.flush_expired_log_deletes()

        assert log_file.exists(), f"status={status} 的日志应保留排障"
        assert pm.read_log_by_pid(pid) is not None
    assert pm._logs_pending_delete == []  # noqa: SLF001 — 非正常退出不入删除队列


# ── 启动清扫：>7 天旧 bash_*.log 一次性清理 ───────────────────────


def _set_mtime(path: Path, age_seconds: float) -> None:
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def test_startup_sweep_removes_only_stale_logs(tmp_path):
    """启动清扫：>7 天的 bash_*.log 被删，新日志与非 bash_ 前缀文件不动。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    stale = log_dir / "bash_1.log"
    stale.write_text("old", encoding="utf-8")
    _set_mtime(stale, age_seconds=8 * 24 * 3600.0)

    fresh = log_dir / "bash_2.log"
    fresh.write_text("new", encoding="utf-8")  # mtime=now

    stale_other = log_dir / "other_3.log"
    stale_other.write_text("old other", encoding="utf-8")
    _set_mtime(stale_other, age_seconds=30 * 24 * 3600.0)

    ProcessManager(log_dir=log_dir)  # 构造即清扫

    assert not stale.exists(), "超 7 天的 bash_*.log 应被清扫"
    assert fresh.exists(), "未过保留期的不动"
    assert stale_other.exists(), "非 bash_ 前缀文件不在清扫范围"


def test_startup_sweep_boundary_at_7_days(tmp_path):
    """清扫边界：恰 7 天+2s（严格大于判定）删除，7 天-1h 保留。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    just_over = log_dir / "bash_10.log"
    just_over.write_text("a", encoding="utf-8")
    _set_mtime(just_over, 7 * 24 * 3600.0 + 2)
    within = log_dir / "bash_11.log"
    within.write_text("b", encoding="utf-8")
    _set_mtime(within, 7 * 24 * 3600.0 - 3600)

    ProcessManager(log_dir=log_dir)

    assert not just_over.exists()
    assert within.exists()


def test_startup_sweep_summary_logged(tmp_path, caplog):
    """清扫带数量汇总日志（scanned/removed），目录无日志文件时静默。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "bash_20.log"
    stale.write_text("x", encoding="utf-8")
    _set_mtime(stale, 10 * 24 * 3600.0)

    with caplog.at_level(logging.INFO, logger="process_manager"):
        ProcessManager(log_dir=log_dir)

    summary = [r for r in caplog.records if "启动日志清扫完成" in r.message]
    assert len(summary) == 1
    assert "scanned=1" in summary[0].message
    assert "removed=1" in summary[0].message

    caplog.clear()
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with caplog.at_level(logging.INFO, logger="process_manager"):
        ProcessManager(log_dir=empty_dir)
    assert not [r for r in caplog.records if "启动日志清扫完成" in r.message]
