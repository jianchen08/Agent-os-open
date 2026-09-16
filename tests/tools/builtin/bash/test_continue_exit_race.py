# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""continue 等待循环跨过退出时点后的三条终局分支补测。

靶行（plugins/shared/tools/bash/tool.py:746-778）：等待循环结束后
``proc_info is None``（记录已被 _on_output_task_done 在「置终态→清内存」
同瞬间移除）时的磁盘回读（746-748）与无日志显式失败（749-752），以及记录
仍在但已转终态时的正常完成路径（766-767/774-775）与非零退出失败路径。

时序构造：进程在 continue 入口为 ``running``（进入等待循环），循环首轮
``asyncio.sleep(0.5)`` 用**真实** sleep 推进，期间记录转终态/被清理，循环
下一拍即观察终局。用真实时钟而非零延迟 mock——判据是「循环跨过终态」，
不依赖墙钟精度，慢机器上同样稳定。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from bash_types import ProcessInfo
from process_manager import ProcessManager
from tool import BashTool

pytestmark = pytest.mark.unit


def _write_log(manager: ProcessManager, pid: int, command: str, body: str) -> Path:
    """按 ProcessManager 命名约定写日志文件（含尾部退出码标记）。"""
    manager.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = manager.log_dir / f"bash_{pid}.log"
    log_file.write_text(
        "# Bash Command Log\n"
        f"# Command: {command}\n"
        f"# PID: {pid}\n"
        "# Started: 2026-01-01T00:00:00\n"
        "# Platform: Linux\n"
        "# ==================================================\n\n"
        f"{body}\n"
        "# Process ended with exit code: 0\n",
        encoding="utf-8",
    )
    return log_file


@pytest.fixture
def tool(tmp_path: Path) -> BashTool:
    t = BashTool()
    t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    return t


def _register(tool: BashTool, pid: int, log_file: Path, command: str) -> ProcessInfo:
    info = ProcessInfo(
        pid=pid,
        command=command,
        start_time=time.time() - 1.0,
        log_file=log_file,
        status="running",
        exit_code=None,
    )
    tool.process_manager.active_processes[pid] = info
    return info


def _stage_after_first_poll(tool: BashTool, pid: int, mutate) -> None:
    """入口探测返回 running（进循环），循环内探测前先执行 mutate 再观察。

    实例属性赋值不绑定 self——补丁签名与真实方法一致（单个 pid 参数）。
    这是对「进程在循环期间转终态」的确定性编排，不是对被测逻辑的替身。
    """
    real = tool.process_manager.get_process_info
    calls = {"n": 0}

    def _staged(pid_: int):
        calls["n"] += 1
        if calls["n"] > 1:
            mutate()
            return tool.process_manager.active_processes.get(pid_)
        return real(pid_)

    tool.process_manager.get_process_info = _staged  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_wait_loop_record_cleared_reads_exit_code_from_disk(tool, tmp_path):
    """等待循环跨过清理时点（proc_info=None）+ 磁盘日志可读 → 回读退出码成功。

    判据：不得把真实退出码兜成 0 之外的失败态，也不得报 PROCESS_NOT_FOUND
    ——磁盘日志是唯一真值源（746-748）。
    """
    pid = 501
    log_file = _write_log(tool.process_manager, pid, "sleep 1; echo done", "done from disk")
    _register(tool, pid, log_file, "sleep 1; echo done")
    _stage_after_first_poll(tool, pid, lambda: tool.process_manager.active_processes.pop(pid, None))

    result = await tool.execute({"action": "continue", "pid": pid, "timeout": 3})

    assert result.success, result.error
    assert result.output["status"] == "completed"
    assert "done from disk" in result.output["output"]
    assert result.output["exit_code"] == 0, "退出码取自磁盘日志尾部标记"
    assert result.metadata["source"] == "file"


@pytest.mark.asyncio
async def test_wait_loop_record_cleared_without_log_fails_explicitly(tool, tmp_path):
    """等待循环跨过清理时点且磁盘无日志 → 显式 PROCESS_NOT_FOUND（749-752）。"""
    pid = 502
    _register(tool, pid, tool.process_manager.log_dir / f"bash_{pid}.log", "gone")
    _stage_after_first_poll(tool, pid, lambda: tool.process_manager.active_processes.pop(pid, None))

    result = await tool.execute({"action": "continue", "pid": pid, "timeout": 3})

    assert not result.success
    assert result.error_code == "PROCESS_NOT_FOUND"
    assert "无法取回退出码" in result.error


@pytest.mark.asyncio
async def test_completed_after_wait_loop_returns_output_and_elapsed(tool, tmp_path):
    """等待循环正常跨到终态（记录仍在）→ 成功态带 output 与 elapsed（766-775）。"""
    pid = 503
    log_file = _write_log(tool.process_manager, pid, "echo hi", "hello after wait loop")
    info = _register(tool, pid, log_file, "echo hi")

    def _complete() -> None:
        info.status = "completed"
        info.exit_code = 0

    _stage_after_first_poll(tool, pid, _complete)

    result = await tool.execute({"action": "continue", "pid": pid, "timeout": 3})

    assert result.success, result.error
    assert result.output["status"] == "completed"
    assert "hello after wait loop" in result.output["output"]
    assert result.output["elapsed"] >= 1.0, "elapsed 由 summary.elapsed_seconds 补回"
    assert result.metadata["action"] == "continue"


@pytest.mark.asyncio
async def test_completed_after_wait_loop_nonzero_exit_reports_failure(tool, tmp_path):
    """等待循环跨到非零退出 → COMMAND_FAILED（对照组：成功分支不是恒真）。"""
    pid = 504
    log_file = _write_log(tool.process_manager, pid, "false", "boom line")
    info = _register(tool, pid, log_file, "false")

    def _fail() -> None:
        info.status = "error"
        info.exit_code = 3

    _stage_after_first_poll(tool, pid, _fail)

    result = await tool.execute({"action": "continue", "pid": pid, "timeout": 3})

    assert not result.success
    assert result.error_code == "COMMAND_FAILED"
    assert result.metadata.get("exit_code") == 3
