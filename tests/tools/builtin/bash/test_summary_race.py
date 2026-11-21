# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""SUMMARY_ERROR 竞态回归测试。

生产 bug：bash("echo alive") 报 "无法获取进程摘要"。

根因：_on_output_task_done 在进程结束时即时清理 active_processes，但
run_command 的轮询循环随后调 get_summary(pid) 时 pid 已被清，
旧 get_summary 返回 None → SUMMARY_ERROR。

快命令（echo）尤其容易触发：_read_output 任务在轮询循环第一次 sleep(0.5)
前就完成并触发清理。

修复：get_summary 在 pid 已清时降级走磁盘（_summary_from_disk），不再返回 None。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tool import BashTool

from agentos_plugin_sdk.process_runtime import ProcessInfo, ProcessManager

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
# bash 工具端到端：竞态不复现
# ============================================================


@pytest.mark.asyncio
async def test_execute_fast_command_no_summary_error(tmp_path):
    """bash 执行快命令（echo）不应报 SUMMARY_ERROR（核心回归）。

    这是生产 bug 的直接复现：echo 几乎瞬间完成，_read_output 任务在
    轮询循环 get_summary 前触发 _on_output_task_done 清理 pid。
    旧代码 get_summary 返回 None → SUMMARY_ERROR。
    """
    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs" / "bash")

    result = await tool.run_command({"command": "echo alive", "timeout": 10, "workspace": str(tmp_path)})

    assert result.success, f"快命令不应失败（竞态清理不应触发 SUMMARY_ERROR），实际: {result.error}"
    assert result.output["status"] == "completed"
    assert "alive" in result.output["output"]


@pytest.mark.asyncio
async def test_execute_fast_command_correct_exit_code(tmp_path):
    """竞态降级后 exit_code 仍正确（从磁盘尾部解析，不是默认 0）。"""
    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs" / "bash")

    # false 命令瞬间退出码 1
    result = await tool.run_command({"command": "false", "timeout": 10, "workspace": str(tmp_path)})

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
        result = await tool.run_command({"command": f"echo line{i}", "timeout": 10, "workspace": str(tmp_path)})
        assert result.success, f"第 {i} 次执行失败: {result.error}"
        assert f"line{i}" in result.output["output"]


# ============================================================
# summary 不可得的降级信封：观测失败不吞调用方结果
# ============================================================


class _SummaryMissingPM:
    """记录已清且磁盘兜底也 miss 的 ProcessManager 桩（get_summary → None）。

    复现 2026-11-12 装机事故（任务 176dd20e32b4，11× SUMMARY_ERROR）的最小
    形态：轮询循环观测到进程结束（记录已被即时清理），get_summary 的内存
    与磁盘降级双 miss。此时 run_command 仍持有 get_output 可得输出与 handle。
    """

    log_dir = Path("/nonexistent-logs")  # 仅作 metadata 现场快照

    async def start_process(self, **kwargs):
        return 4100, None

    def get_process_info(self, pid):
        return None  # 记录已被即时清理 → 轮询循环立即 break

    async def wait_output_settled(self, pid, timeout=5.0):
        return None

    def get_summary(self, pid):
        return None  # 内存记录已清且磁盘兜底 miss

    def get_output(self, pid):
        return "captured-output-line"


@pytest.mark.asyncio
async def test_summary_missing_returns_degraded_envelope(tmp_path):
    """summary 不可得时降级返回 handle+已捕获输出，不再裸 SUMMARY_ERROR。

    生产事故：完成路径 summary 缺失时只抛一句错误，输出与 handle 全部被
    吞——调用方既看不到命令结果也拿不到 handle 去 log 重读，被逼进编造
    handle 的幻觉循环（16 分钟读写打转）。观测失败 ≠ 调用失败：降级信封
    把已捕获的一切如实回传。
    """
    tool = BashTool()
    tool.process_manager = _SummaryMissingPM()

    result = await tool.run_command(
        {"command": "echo hi", "timeout": 10, "workspace": str(tmp_path)}
    )

    assert result.success, f"观测失败应降级不应失败: {result.error}"
    data = result.output
    assert data["handle"], "handle 必须回传（log 重读的唯一凭据）"
    assert "captured-output-line" in data["output"], "已捕获输出必须回传，不得吞掉"
    assert data["status"] == "completed"
    assert "exit_code" not in data, "退出码未知时不得伪造（缺省 0 = 谎报成功）"
    assert data["degraded"] == "summary_unavailable"


@pytest.mark.asyncio
async def test_summary_missing_degraded_metadata_keeps_forensics(tmp_path):
    """降级信封 metadata 带现场（log_dir），下次发生自动留证。"""
    tool = BashTool()
    tool.process_manager = _SummaryMissingPM()

    result = await tool.run_command(
        {"command": "echo hi", "timeout": 10, "workspace": str(tmp_path)}
    )

    assert result.metadata.get("degraded") == "summary_unavailable"
    assert result.metadata.get("log_dir"), "log_dir 现场必须留证（事后归因）"


# ============================================================
# 轮询同步探测先于输出落盘的竞态（wait_output_settled 回归）
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expect"),
    [
        ("echo settled-one", "settled-one"),
        ("echo settled-a && echo settled-b", "settled-b"),
    ],
    ids=["single_line", "compound"],
)
async def test_execute_reads_output_when_poll_detects_exit_before_flush(
    tmp_path, monkeypatch, command, expect
):
    """轮询同步探测先观测到退出、输出尚未 flush 落盘时，完成路径仍读到完整输出。

    复现 CI 高负载竞态：_sync_poll_process 在轮询循环里同步探测（waitpid/
    returncode）把 status 置 completed，而 _read_output 任务还没 flush 落盘——
    旧代码完成路径直接读 summary/output 得到空内容。注入的落盘延迟（1.2s）
    大于轮询周期（0.5s），保证 break 发生在输出落盘之前，确定性触发该窗口。
    """
    original = ProcessManager._read_output

    async def slow_read_output(self, *args, **kwargs):
        await asyncio.sleep(1.2)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ProcessManager, "_read_output", slow_read_output)

    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs" / "bash")

    result = await tool.run_command({"command": command, "timeout": 10, "workspace": str(tmp_path)})

    assert result.success, f"快命令不应失败: {result.error}"
    assert result.output["status"] == "completed"
    assert expect in result.output["output"], (
        "轮询先探测到退出时输出不应丢失（wait_output_settled 未生效）"
    )
