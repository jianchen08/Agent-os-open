"""execute 端到端测试 —— 真实链路，不 mock start_process。

为什么需要这组测试：之前的 execute 测试全 mock 掉了 start_process/get_summary，
只测参数透传，从没跑过真实链路（start_process 写日志 → 进程跑 → 即时清理 →
get_summary 降级读磁盘）。导致生产 SUMMARY_ERROR bug 时 103 个测试全过、没抓住。

这组测试的设计原则：
1. 不 mock start_process——真实起子进程，真实写日志，真实触发即时清理
2. 传 project_root——模拟生产环境（_execute_local_unified 用 project_root/logs/bash
   覆盖 log_dir，这是 log_dir 写读不一致 bug 的触发条件）
3. 用快命令（echo/false）——自然触发竞态（_read_output 任务在轮询循环前完成+清理）
4. 断言 result.success——直接对应生产 bug 的失败现象

这组测试在生产 SUMMARY_ERROR bug 修复禁用时会失败（已验证）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from process_manager import ProcessManager
from tool import BashTool

pytestmark = pytest.mark.unit


def _make_tool_with_project_root(tmp_path: Path) -> BashTool:
    """构造一个 BashTool，模拟生产环境的 log_dir 不一致场景。

    关键：ProcessManager 初始化时用一个"错误"的 log_dir（tmp_path/stale_logs），
    与 project_root 算出的 log_dir（tmp_path/logs/bash）故意不同。
    生产环境正是这样——__init__ 时 _project_root 未知，用默认 logs/bash；
    直到 execute 时才由 _execute_local_unified 传 project_root/logs/bash。

    如果 start_process 不同步 self.log_dir，read_log/get_summary 降级时
    会从 stale_logs 读（找不到 execute 写到 logs/bash 的文件）。
    """
    tool = BashTool()
    # 故意用与 project_root/logs/bash 不同的初始目录
    tool.process_manager = ProcessManager(log_dir=tmp_path / "stale_logs")
    return tool


@pytest.mark.asyncio
async def test_execute_fast_command_with_project_root_no_summary_error(tmp_path):
    """传 project_root 时，快命令不应 SUMMARY_ERROR（生产 bug 直接复现）。

    生产 bug 根因：_execute_local_unified 传 log_dir=project_root/logs/bash 给
    start_process（写入），但 get_summary 降级用 process_manager.log_dir（可能不一致）。
    快命令触发即时清理 → 降级读找不到文件 → SUMMARY_ERROR。
    """
    tool = _make_tool_with_project_root(tmp_path)

    result = await tool.execute({
        "command": "echo alive",
        "timeout": 10,
        "project_root": str(tmp_path),  # 关键：模拟生产注入
    })

    assert result.success, (
        f"传 project_root 时快命令不应失败。生产 bug 现象：{result.error}"
    )
    assert result.output["status"] == "completed"
    assert "alive" in result.output["output"]


@pytest.mark.asyncio
async def test_execute_fast_command_exit_code_preserved_with_project_root(tmp_path):
    """传 project_root 时，快命令的 exit_code 应正确传递（false→exit 1）。

    生产 bug 的另一个表现：即使没 SUMMARY_ERROR，exit_code 也可能丢失
    （降级读磁盘没解析日志尾部的 exit code 标记）。
    """
    tool = _make_tool_with_project_root(tmp_path)

    result = await tool.execute({
        "command": "false",
        "timeout": 10,
        "project_root": str(tmp_path),
    })

    # false 退出码 1，应走 COMMAND_FAILED，不是 SUMMARY_ERROR
    assert not result.success
    assert result.error_code == "COMMAND_FAILED", (
        f"应因 exit_code=1 失败，实际 error_code={result.error_code}"
    )


@pytest.mark.asyncio
async def test_execute_then_read_log_after_cleanup_with_project_root(tmp_path):
    """传 project_root 时：execute 完成（进程已清）后 read_log 仍能读到。

    这是即时清理 + log_dir 一致性的完整验证：
    1. execute 启动 echo，进程跑完被即时清理
    2. pid 从 active_processes 消失
    3. read_log 凭 pid 走磁盘降级
    4. 日志必须能找到（log_dir 写读一致）

    生产 bug 下：execute 后 read_log 报 LOG_FILE_NOT_FOUND（写入目录与读取目录不一致）。
    """
    tool = _make_tool_with_project_root(tmp_path)

    execute_result = await tool.execute({
        "command": "echo persisted_output",
        "timeout": 10,
        "project_root": str(tmp_path),
    })
    assert execute_result.success
    pid = execute_result.output["pid"]

    # 确认进程已被即时清理（active_processes 不再有此 pid）
    assert pid not in tool.process_manager.active_processes, (
        "快命令完成后进程应已被即时清理，否则这测试没触发竞态场景"
    )

    # read_log 凭 pid 走磁盘降级
    read_result = await tool.execute({
        "action": "read_log",
        "pid": pid,
        "project_root": str(tmp_path),
    })

    assert read_result.success, (
        f"execute 后 read_log 应能读到（日志写读一致），实际: {read_result.error}"
    )
    assert "persisted_output" in read_result.output["output"]


@pytest.mark.asyncio
async def test_execute_then_continue_after_cleanup_with_project_root(tmp_path):
    """传 project_root 时：execute 完成（进程已清）后 continue 应降级走磁盘。

    生产 bug 下：continue 报 PROCESS_NOT_FOUND（虽然降级逻辑有，但 log_dir 不一致
    导致 read_log_by_pid 返回 None → PROCESS_NOT_FOUND）。
    """
    tool = _make_tool_with_project_root(tmp_path)

    execute_result = await tool.execute({
        "command": "echo continue_test",
        "timeout": 10,
        "project_root": str(tmp_path),
    })
    assert execute_result.success
    pid = execute_result.output["pid"]

    assert pid not in tool.process_manager.active_processes

    continue_result = await tool.execute({
        "action": "continue",
        "pid": pid,
        "timeout": 5,
        "project_root": str(tmp_path),
    })

    assert continue_result.success, (
        f"execute 后 continue 应降级走磁盘成功，实际: {continue_result.error}"
    )
    assert "continue_test" in continue_result.output["output"]


@pytest.mark.asyncio
async def test_log_dir_consistency_after_start_process(tmp_path):
    """start_process 收到 log_dir 后应同步 self.log_dir（log_dir 写读一致性根因）。

    这是 SUMMARY_ERROR bug 的最底层单元验证：
    - 不传 log_dir → self.log_dir 不变
    - 传 log_dir → self.log_dir 同步成 effective_log_dir
    两者一致才能保证 read_log_by_pid（用 self.log_dir）能读到 start_process 写的文件。
    """
    import asyncio

    pm = ProcessManager(log_dir=tmp_path / "initial_logs")
    initial_log_dir = pm.log_dir

    external_log_dir = tmp_path / "external_logs" / "bash"
    pid, log_file = await pm.start_process(
        command="echo test",
        log_dir=external_log_dir,
    )

    # 等进程结束
    await asyncio.sleep(0.5)

    assert pm.log_dir == external_log_dir.resolve(), (
        f"start_process 收到 log_dir 后应同步 self.log_dir。"
        f"期望 {external_log_dir.resolve()}，实际 {pm.log_dir}"
    )

    # read_log_by_pid 用 self.log_dir 算路径，必须能找到文件
    file_data = pm.read_log_by_pid(pid)
    assert file_data is not None, (
        f"start_process 写入 {log_file}，read_log_by_pid 用 self.log_dir={pm.log_dir} "
        f"算路径应能找到。log_dir 写读不一致是 SUMMARY_ERROR 的根因。"
    )


@pytest.mark.asyncio
async def test_multiple_fast_commands_with_project_root(tmp_path):
    """连续多个快命令（都传 project_root）都不应触发竞态（压力测试）。

    生产环境 LLM 会连续调 execute（日志里 echo alive 连调 3 次都失败）。
    """
    tool = _make_tool_with_project_root(tmp_path)

    for i in range(5):
        result = await tool.execute({
            "command": f"echo line{i}",
            "timeout": 10,
            "project_root": str(tmp_path),
        })
        assert result.success, f"第 {i} 次执行失败（生产 bug 复现）: {result.error}"
        assert f"line{i}" in result.output["output"]
