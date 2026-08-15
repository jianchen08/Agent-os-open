"""read_log 双路径测试。

read_log 应在两种场景都能工作：
1. 进程活跃（在 active_processes）→ 从内存读，含实时 status
2. 进程已清（completed 后被 _on_output_task_done 清理）→ 按 pid 算路径读磁盘

同时验证 LOG_FILE_NOT_FOUND 失败路径和 command 解析（用于 LogCompressor 类型推断）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bash_types import ProcessInfo
from process_manager import ProcessManager
from tool import BashTool

pytestmark = pytest.mark.unit


@pytest.fixture
def pm(tmp_path):
    return ProcessManager(log_dir=tmp_path / "logs")


@pytest.fixture
def tool(tmp_path):
    """BashTool 实例，日志写到临时目录。"""
    t = BashTool()
    # 让 process_manager 用临时目录，避免污染真实 logs/bash
    t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    return t


def _write_log_file(log_dir: Path, pid: int, command: str, content: str) -> Path:
    """按生产格式写一个磁盘日志文件（含头部 # Command:）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"bash_{pid}.log"
    header = f"# Bash Command Log\n# Command: {command}\n# PID: {pid}\n# Started: 2026-01-01T00:00:00\n# Platform: Linux\n# ==================================================\n\n"
    log_file.write_text(header + content, encoding="utf-8")
    return log_file


# ============================================================
# read_log_by_pid 辅助方法（磁盘路径）
# ============================================================


def test_read_log_by_pid_returns_none_when_file_missing(pm, tmp_path):
    """文件不存在时返回 None。"""
    result = pm.read_log_by_pid(99999)
    assert result is None


def test_read_log_by_pid_reads_content_and_parses_command(pm, tmp_path):
    """磁盘日志应被读出 output，并从 # Command: 头部解析 command。"""
    _write_log_file(
        tmp_path / "logs",
        pid=12345,
        command="npm install express lodash",
        content="added 2 packages in 1s\n",
    )

    result = pm.read_log_by_pid(12345)
    assert result is not None
    assert "added 2 packages" in result["output"]
    assert result["command"] == "npm install express lodash"


def test_read_log_by_pid_summary_includes_type_inference(pm, tmp_path):
    """LogCompressor 应根据 command 推断输出类型（npm install 等）。"""
    _write_log_file(
        tmp_path / "logs",
        pid=12346,
        command="npm install react",
        content=("".join(f"added package {i}\n" for i in range(20))),
    )

    result = pm.read_log_by_pid(12346)
    assert result is not None
    # summary 应包含类型信息（LogCompressor 检测到 npm install）
    summary_text = " ".join(result["summary"])
    assert "npm install" in summary_text


def test_read_log_by_pid_strips_header_lines(pm, tmp_path):
    """output 应不含 # 头部行，只含实际输出。"""
    _write_log_file(
        tmp_path / "logs",
        pid=12347,
        command="echo hi",
        content="real output line\n",
    )

    result = pm.read_log_by_pid(12347)
    assert result is not None
    assert "# Bash Command Log" not in result["output"]
    assert "# Command:" not in result["output"]
    assert "real output line" in result["output"]


# ============================================================
# _handle_read_log 双路径
# ============================================================


@pytest.mark.asyncio
async def test_read_log_active_process_from_memory(tool, tmp_path):
    """进程活跃（在 active_processes）→ 走内存路径，返回实时 status。"""
    log_file = tmp_path / "logs" / "bash_42.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("running output line\n", encoding="utf-8")

    # 注入一个活跃进程到 active_processes
    info = ProcessInfo(
        pid=42,
        command="some running cmd",
        start_time=0,
        log_file=log_file,
        status="running",
    )
    tool.process_manager.active_processes[42] = info

    result = await tool.execute({"action": "read_log", "pid": 42})

    assert result.success
    assert result.output["status"] == "running"
    assert result.output["pid"] == 42
    assert "running output line" in result.output["output"]
    assert result.metadata["source"] == "memory"


@pytest.mark.asyncio
async def test_read_log_cleared_process_from_disk(tool, tmp_path):
    """进程已清（不在 active_processes）→ 自动走磁盘路径。"""
    _write_log_file(
        tmp_path / "logs",
        pid=7777,
        command="grep -r foo .",
        content="path/to/file.py:foo = 1\npath/to/other.py:foo = 2\n",
    )
    # 确保 active_processes 里没有这个 pid（模拟已被即时清理）
    assert 7777 not in tool.process_manager.active_processes

    result = await tool.execute({"action": "read_log", "pid": 7777})

    assert result.success
    assert result.output["status"] == "completed"  # 磁盘读到的都是已结束的
    assert result.output["pid"] == 7777
    assert "foo = 1" in result.output["output"]
    assert result.metadata["source"] == "file"


@pytest.mark.asyncio
async def test_read_log_missing_pid_and_no_file_returns_failure(tool):
    """pid 不存在且无日志文件 → LOG_FILE_NOT_FOUND 失败。"""
    result = await tool.execute({"action": "read_log", "pid": 88888})

    assert not result.success
    assert result.error_code == "LOG_FILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_read_log_no_pid_returns_failure(tool):
    """没传 pid → MISSING_PID 失败。"""
    result = await tool.execute({"action": "read_log"})

    assert not result.success
    assert result.error_code == "MISSING_PID"
