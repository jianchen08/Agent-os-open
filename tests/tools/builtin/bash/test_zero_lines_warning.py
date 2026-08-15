"""[0行] bug 告警测试。

背景：长任务（编译类）由于子进程 stdout 块缓冲，日志文件长时间为空，
LogCompressor 拿到空 lines → 平淡输出 "[0行]"，LLM 误判为"正常无输出"。
修复：get_summary 在 elapsed>15s 且 total_lines==0 时插入告警行。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from bash_types import ProcessInfo
from process_manager import ProcessManager

pytestmark = pytest.mark.unit


@pytest.fixture
def pm(tmp_path):
    return ProcessManager(log_dir=tmp_path / "logs")


def _make_info(pid: int, start_time: float, log_file: Path) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        command="cargo build",
        start_time=start_time,
        log_file=log_file,
        status="running",
    )


def test_zero_lines_long_elapsed_inserts_warning(pm, tmp_path):
    """elapsed > 15s 且 total_lines == 0 → summary 应含告警行。"""
    log_file = tmp_path / "bash_200.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")  # 空日志

    # start_time 设为 30 秒前
    pm.active_processes[200] = _make_info(200, time.time() - 30, log_file)

    summary = pm.get_summary(200)

    assert summary is not None
    summary_text = " ".join(summary["summary"])
    assert "⚠️" in summary_text, "应插入告警"
    assert "已运行" in summary_text
    assert "30s" in summary_text


def test_zero_lines_short_elapsed_no_warning(pm, tmp_path):
    """elapsed < 15s 且 total_lines == 0 → 不插入告警（短任务正常无输出）。"""
    log_file = tmp_path / "bash_201.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")

    # start_time 设为 5 秒前
    pm.active_processes[201] = _make_info(201, time.time() - 5, log_file)

    summary = pm.get_summary(201)

    assert summary is not None
    summary_text = " ".join(summary["summary"])
    assert "⚠️" not in summary_text


def test_nonzero_lines_no_warning_even_long_elapsed(pm, tmp_path):
    """有输出时（total_lines > 0）不插入告警，即使 elapsed 很大。"""
    log_file = tmp_path / "bash_202.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("Compiling foo v0.1\nCompiling bar v0.1\n", encoding="utf-8")

    # start_time 设为 100 秒前
    pm.active_processes[202] = _make_info(202, time.time() - 100, log_file)

    summary = pm.get_summary(202)

    assert summary is not None
    summary_text = " ".join(summary["summary"])
    assert "⚠️" not in summary_text


def test_warning_inserted_at_summary_head(pm, tmp_path):
    """告警行应在 summary.lines 的第一个位置（最醒目）。"""
    log_file = tmp_path / "bash_203.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")

    pm.active_processes[203] = _make_info(203, time.time() - 20, log_file)

    summary = pm.get_summary(203)

    assert summary is not None
    assert summary["summary"][0].startswith("⚠️"), "告警应在第一行"
