"""真实命令端到端测试：验证退出码/信号/输出形态在完整链路下的行为。

设计原则（对齐 test_execute_e2e.py）：
1. 不 mock start_process——真实起子进程，真实写日志
2. 覆盖各种真实命令形态：
   - 短输出成功（echo / ls 单文件）
   - 长输出成功（seq 刷屏，触发字符阈值）
   - 短错误（false / 不存在的命令）
   - 长输出含错误（编译式刷屏 + 末尾 error）
   - 管道失败冒泡（false | true，验证 pipefail）
   - 被信号终止（kill -9 $$ → exit 137，模拟 OOM）
   - 末尾 Killed 字样（模拟 OOM Killer 的真实输出形态）

这组测试验证：失败信息按"短全量、长提取"原则组装，信号被如实暴露，
管道失败不被最后一条命令的成功码掩盖。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.builtin.bash.tool import BashTool


def _make_tool(tmp_path: Path) -> BashTool:
    """构造 BashTool，模拟生产 log_dir（对齐 test_execute_e2e 的构造方式）。"""
    from tools.builtin.bash.process_manager import ProcessManager

    tool = BashTool()
    tool.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    return tool


# ── 成功路径：短 / 长输出 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_short_success_returns_full_output(tmp_path):
    """短输出成功：output 全量返回，无 summary（字符 < 阈值）。"""
    tool = _make_tool(tmp_path)
    result = await tool.execute({
        "command": "echo hello_world",
        "timeout": 10,
        "project_root": str(tmp_path),
    })
    assert result.success
    assert "hello_world" in result.output["output"]
    assert "summary" not in result.output, "短输出不应有 summary（噪音）"


@pytest.mark.asyncio
async def test_long_success_includes_summary(tmp_path):
    """长输出成功：output 全量 + 附 summary（筛掉刷屏噪音）。"""
    tool = _make_tool(tmp_path)
    # seq 200 行 ~ 1000+ 字符，但需超 2000 字符阈值才触发 summary
    result = await tool.execute({
        "command": "for i in $(seq 1 300); do echo \"line $i with padding text to exceed threshold\"; done",
        "timeout": 15,
        "project_root": str(tmp_path),
    })
    assert result.success
    assert "summary" in result.output, "长输出应有 summary"
    # summary 含行数统计
    summary_text = " ".join(result.output["summary"])
    assert "行" in summary_text


# ── 错误路径：短 / 长 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_error_returns_full_output(tmp_path):
    """短错误：退出码 + 全量原文（信息量小，截断反而切坏）。"""
    tool = _make_tool(tmp_path)
    result = await tool.execute({
        "command": "echo 'error: something failed' >&2; exit 3",
        "timeout": 10,
        "project_root": str(tmp_path),
    })
    assert not result.success
    assert result.error_code == "COMMAND_FAILED"
    assert "退出码: 3" in result.error
    assert "something failed" in result.error, "短错误应全量返回原文"


@pytest.mark.asyncio
async def test_long_error_with_noise_uses_extracted_errors(tmp_path):
    """长输出含错误：优先用提取的错误行，筛掉刷屏噪音。

    模拟编译场景：大量 transforming/进度行（噪音）+ 末尾真正的 error 行。
    旧实现 output[-500:] 可能只截到噪音，真正的 error 行被挤出窗口。
    """
    tool = _make_tool(tmp_path)
    # 刷屏噪音 + 末尾错误
    result = await tool.execute({
        "command": (
            "for i in $(seq 1 200); do echo \"transforming module $i...\"; done; "
            "echo 'error: cannot find module foo'; exit 1"
        ),
        "timeout": 15,
        "project_root": str(tmp_path),
    })
    assert not result.success
    # 关键错误行必须在 error 里（不能被噪音淹没）
    assert "cannot find module foo" in result.error


# ── 管道失败冒泡（pipefail）────────────────────────────────────


@pytest.mark.asyncio
async def test_pipe_failure_propagates_with_pipefail(tmp_path):
    """管道里前段失败时，退出码应反映失败（pipefail），而非最后一段的成功码。

    `false | true`：无 pipefail 时退出码=0（true 成功），有 pipefail 时退出码=1。
    旧实现无 pipefail → 工具误报成功，掩盖管道里的失败。
    """
    tool = _make_tool(tmp_path)
    result = await tool.execute({
        "command": "false | true",
        "timeout": 10,
        "project_root": str(tmp_path),
    })
    assert not result.success, (
        "pipefail 下 `false | true` 应失败（退出码 1），旧实现无 pipefail 会误报成功"
    )


# ── 被信号终止（模拟 OOM）──────────────────────────────────────


@pytest.mark.asyncio
async def test_sigkill_exit_exposes_signal(tmp_path):
    """被信号终止（退出码 137=SIGKILL，模拟 OOM）→ 信号如实暴露。

    生产环境是 Linux 容器，OOM 被 SIGKILL → 退出码 137（128+9）。
    本测试用 exit 137 模拟该退出码（Windows bash 的 kill -9 退出码是 9 而非
    137，与 Linux POSIX 语义不同，故用 exit 精确模拟）。

    旧实现：exit_code=137 只是个孤立数字，LLM 不知道意味着"被信号杀"。
    修复后：error 含信号描述（SIGKILL），metadata 含 terminated_by_signal。
    """
    tool = _make_tool(tmp_path)
    result = await tool.execute({
        "command": "exit 137",
        "timeout": 10,
        "project_root": str(tmp_path),
    })
    assert not result.success
    assert result.error_code == "COMMAND_FAILED"
    assert "SIGKILL" in result.error, (
        f"被 SIGKILL 终止（exit 137）应暴露信号名，实际 error: {result.error}"
    )
    assert "137" in result.error


@pytest.mark.asyncio
async def test_killed_text_recognized_as_error(tmp_path):
    """输出含 "Killed" 字样（OOM Killer 真实形态）应被识别为错误行。

    模拟：vite build 输出 ...transforming... 后被 OOM 杀，末尾出现 "Killed"。
    旧 ERROR_PATTERNS 不识别 Killed → 错误列表为空 → LLM 拿到 137 却找不到对应错误。
    用 exit 137 模拟信号退出码（见上测试说明）。
    """
    tool = _make_tool(tmp_path)
    # 构造长输出（噪音）+ 末尾 Killed，然后退出 137
    result = await tool.execute({
        "command": (
            "for i in $(seq 1 200); do echo \"transforming module $i...\"; done; "
            "echo 'Killed'; exit 137"
        ),
        "timeout": 15,
        "project_root": str(tmp_path),
    })
    assert not result.success
    assert "SIGKILL" in result.error
    # Killed 字样应出现在返回信息里（长输出兜底到末尾原始输出）
    assert "Killed" in result.error


@pytest.mark.asyncio
async def test_command_not_found_recognized(tmp_path):
    """command not found 应被识别为错误并出现在返回信息里。"""
    tool = _make_tool(tmp_path)
    result = await tool.execute({
        "command": "this_command_does_not_exist_xyz",
        "timeout": 10,
        "project_root": str(tmp_path),
    })
    assert not result.success
    assert "not found" in result.error.lower() or "退出码" in result.error


# ── 长任务轮询：running 中间态返回高价值信息 ──────────────────


@pytest.mark.asyncio
async def test_running_mid_state_returns_output_not_just_summary(tmp_path):
    """长任务轮询超时返回 running 时，应带 output 尾部（而非只有 [N行] 摘要）。

    旧实现 running 中间态只塞 status/pid/elapsed/summary，丢掉 output——
    agent 反复 continue 只看到"[0行]"或"已运行 N s 无输出"，看不到进程在干啥，
    得另外调 read_log。修复后 running 态与 completed 态走同一套提取逻辑：
    短输出全量、长输出尾部 + latest_message/summary。
    """
    tool = _make_tool(tmp_path)
    # 跑一个持续输出 3 秒的长任务，timeout 设 1 秒让它在 running 中间态返回
    result = await tool.execute({
        "command": "for i in $(seq 1 60); do echo \"working step $i\"; sleep 0.05; done",
        "timeout": 1,
        "project_root": str(tmp_path),
    })
    assert result.success, "running 中间态应返回 success（status=running）"
    assert result.output["status"] == "running"
    # 关键：output 字段必须有内容（旧实现只有 summary，无 output）
    assert "output" in result.output, "running 态应返回 output（旧实现丢失）"
    assert result.output["output"], "output 不应为空"
    # 实时进度应能看到
    assert "working step" in result.output["output"]


@pytest.mark.asyncio
async def test_running_continue_returns_enriched_output(tmp_path):
    """continue 轮询 running 进程时也应返回 output 尾部（与 execute 一致）。"""
    tool = _make_tool(tmp_path)
    # 先 execute 起 3 秒长任务，1 秒超时拿到 pid（running 态）
    exec_result = await tool.execute({
        "command": "for i in $(seq 1 60); do echo \"progress $i\"; sleep 0.05; done",
        "timeout": 1,
        "project_root": str(tmp_path),
    })
    assert exec_result.output["status"] == "running"
    pid = exec_result.output["pid"]

    # continue 再轮询（短 timeout，进程仍在跑）
    cont_result = await tool.execute({
        "action": "continue",
        "pid": pid,
        "timeout": 1,
        "project_root": str(tmp_path),
    })
    assert cont_result.success
    # continue 的 running 态也要带 output（旧实现只返回 summary）
    assert "output" in cont_result.output
    assert cont_result.output["output"], "continue running 态 output 不应为空"


@pytest.mark.asyncio
async def test_running_long_output_tail_complete_lines(tmp_path):
    """长输出 running 态：output 尾部每行完整（不切到行中间）。"""
    tool = _make_tool(tmp_path)
    # 快速产出大量输出（超 2000 字符阈值），timeout 设小让它 running 返回
    result = await tool.execute({
        "command": "for i in $(seq 1 500); do echo \"line $i padding to make it long enough\"; done; sleep 3",
        "timeout": 1,
        "project_root": str(tmp_path),
    })
    assert result.output["status"] == "running"
    out = result.output.get("output", "")
    # output 被截成尾部，但每行必须完整（以 "line N" 开头）
    lines = [l for l in out.split("\n") if l.strip()]
    for line in lines:
        assert line.startswith("line "), f"行被切断或不完整: {line[:40]}"

