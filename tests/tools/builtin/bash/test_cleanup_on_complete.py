"""completed 进程即时清理测试。

_on_output_task_done 在进程输出读完（=进程已退出）时，应立即从 active_processes
清理内存记录；日志文件保留磁盘（read_log 按 pid 算路径读）。
"""

from __future__ import annotations

import asyncio
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
    # 不应抛异常
    pm._on_output_task_done(99999, _DoneTask())


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
