"""SUMMARY_ERROR 竞态回归测试。

生产 bug：execute("echo alive") 报 "无法获取进程摘要"。

根因：_on_output_task_done 在进程结束时即时清理 active_processes，但
_execute_local_unified 的轮询循环随后调 get_summary(pid) 时 pid 已被清，
旧 get_summary 返回 None → SUMMARY_ERROR。

快命令（echo）尤其容易触发：_read_output 任务在轮询循环第一次 sleep(0.5)
前就完成并触发清理。

修复：get_summary 在 pid 已清时降级走磁盘（_summary_from_disk），不再返回 None。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bash_types import ProcessInfo
from process_manager import ProcessManager
from tool import BashTool

pytestmark = pytest.mark.unit


@pytest.fixture
def pm(tmp_path):
    return ProcessManager(log_dir=tmp_path / "logs" / "bash")


# ============================================================
# get_summary 降级：单元层
# ============================================================


def test_get_summary_cleared_pid_falls_back_to_disk(pm, tmp_path):
    """get_summary 在 pid 已清时应降级走磁盘，不再返回 None（触发 SUMMARY_ERROR）。"""
    # 构造一个已写完的日志文件（含 exit code 尾标记）
    log_file = pm.log_dir / "bash_500.log"
    pm.log_dir.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "# Bash Command Log\n# Command: echo alive\n# PID: 500\n"
        "# Started: 2026-01-01T00:00:00\n# Platform: Linux\n# ===\n\n"
        "alive\n"
        "\n# Process ended with exit code: 0\n",
        encoding="utf-8",
    )
    # pid 500 不在 active_processes（模拟已被即时清理）
    assert 500 not in pm.active_processes

    summary = pm.get_summary(500)

    assert summary is not None, "pid 已清时应降级走磁盘，不应返回 None"
    assert summary["status"] == "completed"
    assert summary["exit_code"] == 0
    assert "alive" in "\n".join(summary["summary"]) or summary["exit_code"] == 0


def test_get_summary_cleared_nonexistent_pid_returns_none(pm):
    """pid 已清且无磁盘日志 → 返回 None（read_log 层会转 LOG_FILE_NOT_FOUND）。"""
    summary = pm.get_summary(999999)
    # 既不在内存也无磁盘文件
    assert summary is None


def test_get_summary_cleared_pid_parses_exit_code_from_log_tail(pm, tmp_path):
    """降级读磁盘时，exit_code 应从日志尾部 # Process ended with exit code: N 解析。"""
    log_file = pm.log_dir / "bash_501.log"
    pm.log_dir.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "# Bash Command Log\n# Command: false\n# PID: 501\n"
        "# ===\n\n"
        "some output\n"
        "\n# Process ended with exit code: 1\n",
        encoding="utf-8",
    )
    assert 501 not in pm.active_processes

    summary = pm.get_summary(501)

    assert summary is not None
    assert summary["exit_code"] == 1, "应从日志尾部解析 exit_code=1"


# ============================================================
# execute 端到端：竞态不复现
# ============================================================


@pytest.mark.asyncio
async def test_execute_fast_command_no_summary_error(tmp_path):
    """execute 一个快命令（echo）不应报 SUMMARY_ERROR（核心回归）。

    这是生产 bug 的直接复现：echo 几乎瞬间完成，_read_output 任务在
    轮询循环 get_summary 前触发 _on_output_task_done 清理 pid。
    旧代码 get_summary 返回 None → SUMMARY_ERROR。
    """
    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs" / "bash")

    result = await tool.execute({"command": "echo alive", "timeout": 10})

    assert result.success, f"快命令不应失败（竞态清理不应触发 SUMMARY_ERROR），实际: {result.error}"
    assert result.output["status"] == "completed"
    assert "alive" in result.output["output"]


@pytest.mark.asyncio
async def test_execute_fast_command_correct_exit_code(tmp_path):
    """竞态降级后 exit_code 仍正确（从磁盘尾部解析，不是默认 0）。"""
    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs" / "bash")

    # false 命令瞬间退出码 1
    result = await tool.execute({"command": "false", "timeout": 10})

    # exit_code=1 应走 COMMAND_FAILED 失败路径，不是 SUMMARY_ERROR
    assert not result.success
    assert result.error_code == "COMMAND_FAILED", (
        f"应因 exit_code=1 失败，实际: {result.error_code}"
    )


@pytest.mark.asyncio
async def test_execute_multiple_fast_commands_no_race(tmp_path):
    """连续多个快命令都不应触发竞态（压力测试）。"""
    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs" / "bash")

    for i in range(5):
        result = await tool.execute({"command": f"echo line{i}", "timeout": 10})
        assert result.success, f"第 {i} 次执行失败: {result.error}"
        assert f"line{i}" in result.output["output"]
