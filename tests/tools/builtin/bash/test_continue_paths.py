"""continue 路径测试。

验证两个新增/改动行为：
1. continue 完成路径返回 output 字段（对齐 execute 完成路径，原只有 summary）
2. continue 在 pid 已清（completed 即时清理后）时降级走磁盘日志，而非粗暴失败
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bash_types import ProcessInfo
from process_manager import ProcessManager
from tool import BashTool

pytestmark = pytest.mark.unit


@pytest.fixture
def tool(tmp_path):
    t = BashTool()
    t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    return t


def _write_log_file(log_dir: Path, pid: int, command: str, content: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"bash_{pid}.log"
    header = (
        f"# Bash Command Log\n# Command: {command}\n# PID: {pid}\n"
        f"# Started: 2026-01-01T00:00:00\n# Platform: Linux\n"
        f"# ==================================================\n\n"
    )
    log_file.write_text(header + content, encoding="utf-8")
    return log_file


@pytest.mark.asyncio
async def test_continue_completed_includes_output(tool, tmp_path):
    """continue 完成路径应返回 output 字段（不只是 summary）。"""
    log_file = _write_log_file(
        tmp_path / "logs", pid=300, command="echo done",
        content="real result line\n",
    )
    info = ProcessInfo(
        pid=300,
        command="echo done",
        start_time=0,
        log_file=log_file,
        status="completed",
        exit_code=0,
    )
    tool.process_manager.active_processes[300] = info

    # timeout 设小，第一次轮询就发现进程已完成（status != running 直接返回）
    result = await tool.execute({"action": "continue", "pid": 300, "timeout": 1})

    assert result.success
    assert result.output["status"] == "completed"
    assert "real result line" in result.output["output"], "完成路径应含 output"
    assert "summary" in result.output or "output" in result.output  # 至少有其一


@pytest.mark.asyncio
async def test_continue_cleared_pid_falls_back_to_disk(tool, tmp_path):
    """continue 在 pid 已清时应降级走磁盘，而非 PROCESS_NOT_FOUND。

    场景：execute 启动→进程完成被即时清理→LLM 调 continue 想确认结果。
    应自动从磁盘读最后输出，告诉 LLM "进程已结束"+输出。
    """
    _write_log_file(
        tmp_path / "logs", pid=301, command="some completed cmd",
        content="final output line 1\nfinal output line 2\n",
    )
    # 确保 active_processes 里没有这个 pid（模拟已即时清理）
    assert 301 not in tool.process_manager.active_processes

    result = await tool.execute({"action": "continue", "pid": 301, "timeout": 1})

    assert result.success, "应降级成功，而非失败"
    assert result.output["status"] == "completed"
    assert "final output line 1" in result.output["output"]
    assert result.metadata["source"] == "file"


@pytest.mark.asyncio
async def test_continue_no_pid_no_file_returns_failure(tool):
    """pid 不存在且无磁盘日志 → PROCESS_NOT_FOUND 失败。"""
    result = await tool.execute({"action": "continue", "pid": 99998, "timeout": 1})

    assert not result.success
    assert result.error_code == "PROCESS_NOT_FOUND"


@pytest.mark.asyncio
async def test_continue_missing_pid_arg_returns_failure(tool):
    """没传 pid → MISSING_PID 失败。"""
    result = await tool.execute({"action": "continue", "timeout": 1})

    assert not result.success
    assert result.error_code == "MISSING_PID"
