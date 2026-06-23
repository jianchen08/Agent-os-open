"""Bash 工具集成测试 — 真实 subprocess 端到端验证。

覆盖：
  Bug1: send_input 交互式输入
  Bug2: terminate_process 终止
  Bug3: shell 变量展开（wsl -e bash -c）
"""

from __future__ import annotations

import asyncio

import pytest

from tools.builtin.bash.process_manager import ProcessManager
from tools.builtin.bash.types import ProcessInfo


# ============================================================
# Helpers
# ============================================================

def _stdout_only(output: str) -> str:
    """从 get_output 中提取纯 stdout（排除 [stderr] 行和 WSL 启动噪音）。"""
    lines = output.splitlines()
    stdout_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[stderr]"):
            continue
        stdout_lines.append(stripped)
    return "\n".join(stdout_lines)


async def _wait_for_process(
    pm: ProcessManager, pid: int, timeout: float = 15,
) -> ProcessInfo:
    """等待进程完成，返回 ProcessInfo。"""
    start = asyncio.get_event_loop().time()
    while True:
        info = pm.get_process_info(pid)
        if info is None:
            raise RuntimeError(f"Process {pid} not found")
        if info.status in ("completed", "error", "terminated"):
            return info
        if asyncio.get_event_loop().time() - start > timeout:
            try:
                await pm.terminate_process(pid, force=True)
            except Exception:
                pass
            raise TimeoutError(f"Process {pid} did not finish in {timeout}s")
        await asyncio.sleep(0.1)


@pytest.fixture
def pm(tmp_path):
    """创建 ProcessManager 实例，日志写入临时目录。"""
    return ProcessManager(log_dir=tmp_path / "logs" / "bash")


# ============================================================
# Bug3: Shell 变量展开
# ============================================================

class TestShellVariableExpansion:
    """验证 i=5; echo $i / for 循环等 Unix shell 语法。"""

    @pytest.mark.asyncio
    async def test_simple_variable_expansion(self, pm):
        """i=5; echo $i → 输出应包含 5"""
        pid, _ = await pm.start_process("i=5; echo $i")
        proc_info = await _wait_for_process(pm, pid)

        output = _stdout_only(pm.get_output(pid))
        assert proc_info.exit_code == 0, f"exit_code={proc_info.exit_code}, output={output}"
        assert "5" in output, f"Expected '5' in output, got: {output}"

    @pytest.mark.asyncio
    async def test_for_loop_expansion(self, pm):
        """for j in 1 2 3; do echo $j; done → 输出 1 2 3"""
        pid, _ = await pm.start_process("for j in 1 2 3; do echo $j; done")
        proc_info = await _wait_for_process(pm, pid)

        output = _stdout_only(pm.get_output(pid))
        assert proc_info.exit_code == 0, f"exit_code={proc_info.exit_code}"
        assert "1" in output and "2" in output and "3" in output, f"Output: {output}"

    @pytest.mark.asyncio
    async def test_multiline_script(self, pm):
        """多行 shell 脚本（管道 + 变量）"""
        pid, _ = await pm.start_process(
            "count=0; for f in a b c; do count=$((count+1)); done; echo $count"
        )
        proc_info = await _wait_for_process(pm, pid)

        output = _stdout_only(pm.get_output(pid))
        assert proc_info.exit_code == 0
        assert "3" in output, f"Expected '3' in output, got: {output}"


# ============================================================
# Bug1: send_input 交互式输入
# ============================================================

class TestInteractiveInput:
    """验证向运行中的进程发送输入。"""

    @pytest.mark.asyncio
    async def test_send_input_to_read(self, pm):
        """read 等待输入 → send_input → 验证输出"""
        pid, _ = await pm.start_process(
            'bash -c \'read answer; echo "got: $answer"\''
        )
        await asyncio.sleep(0.5)

        ok, err = await pm.send_input(pid, "hello")
        assert ok, f"send_input failed: {err}"

        proc_info = await _wait_for_process(pm, pid)
        output = _stdout_only(pm.get_output(pid))
        assert proc_info.exit_code == 0, f"exit_code={proc_info.exit_code}, output={output}"
        assert "hello" in output, f"Expected 'hello' in output, got: {output}"

    @pytest.mark.asyncio
    async def test_send_input_to_cat(self, pm):
        """cat 等待 stdin → send_input → cat 回显"""
        pid, _ = await pm.start_process("cat")
        await asyncio.sleep(0.3)

        ok, err = await pm.send_input(pid, "test_string")
        assert ok, f"send_input failed: {err}"

        try:
            proc_info = pm.active_processes[pid]
            if proc_info.process and proc_info.process.stdin:
                proc_info.process.stdin.close()
        except Exception:
            pass

        proc_info = await _wait_for_process(pm, pid)
        output = _stdout_only(pm.get_output(pid))
        assert "test_string" in output, f"Expected 'test_string' in output, got: {output}"

    @pytest.mark.asyncio
    async def test_send_input_rejects_on_finished(self, pm):
        """已完成进程拒绝 send_input"""
        pid, _ = await pm.start_process("echo done")
        await _wait_for_process(pm, pid)

        ok, err = await pm.send_input(pid, "should fail")
        assert not ok
        assert any(kw in (err or "").lower() for kw in ("状态", "status", "已结束"))


# ============================================================
# Bug2: terminate_process 终止
# ============================================================

class TestTerminateProcess:
    """验证进程终止功能。"""

    @pytest.mark.asyncio
    async def test_terminate_running(self, pm):
        """启动 sleep → terminate → 验证状态"""
        pid, _ = await pm.start_process("sleep 30")
        await asyncio.sleep(0.5)

        ok, err = await pm.terminate_process(pid, force=True)
        assert ok, f"terminate failed: {err}"

        proc_info = pm.get_process_info(pid)
        assert proc_info is not None
        assert proc_info.status == "terminated"

    @pytest.mark.asyncio
    async def test_terminate_nonexistent(self, pm):
        """不存在的 PID 终止应报错"""
        ok, err = await pm.terminate_process(99999)
        assert not ok


# ============================================================
# Bug1 补充: loop-closed 场景
# ============================================================

class TestSendInputLoopClosed:
    """事件循环已关闭时的 send_input（超时返回后场景）。"""

    @pytest.mark.asyncio
    async def test_send_input_after_loop_closed(self, pm):
        """send_input 在主循环执行，不再有跨循环问题"""
        pid, _ = await pm.start_process("cat")
        await asyncio.sleep(0.3)

        ok, err = await pm.send_input(pid, "main_loop_test")
        assert ok, f"send_input failed: {err}"

        try:
            proc_info = pm.active_processes[pid]
            if proc_info.process and proc_info.process.stdin:
                proc_info.process.stdin.close()
        except Exception:
            pass

        proc_info = await _wait_for_process(pm, pid, timeout=15)
        output = _stdout_only(pm.get_output(pid))
        assert "main_loop_test" in output, f"Expected 'main_loop_test' in output, got: {output}"
