"""_exec_in_container 进程组包裹 + 超时整组杀 + TimeoutExpired 修复测试。

背景（setns 故障的进程泄漏根因）：
docker exec 超时时，旧实现 _run_cmd 用 subprocess.run(timeout=...) 只杀本地
docker exec 客户端进程，容器内的 cargo/rustc 后代变孤儿继续跑 → 僵尸堆积 →
PidsLimit 耗尽 → runc setns 崩溃。修复：
1. 命令用 setsid 包裹自成进程组，后代归同一 PGID 可整组寻址。
2. 超时时用 ContainerProcessBackend.kill 整组杀（docker exec kill -- -PGID）。
3. subprocess.TimeoutExpired 非 TimeoutError，旧 except TimeoutError 接不住，
   超时被误报为"执行命令失败"——改为显式捕获 TimeoutExpired。

本测试 mock _run_cmd，验证上述三点，不碰真实 docker。
"""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock

import pytest

from isolation.providers.docker_provider import DockerProvider
from isolation.types import ExecutionResult


def _make_provider() -> DockerProvider:
    """构造 DockerProvider，_run_cmd 为可控 mock。"""
    provider = DockerProvider()
    provider._run_cmd = AsyncMock()
    return provider


# ---------------------------------------------------------------------------
# 1. 命令用 setsid 包裹自成进程组
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_wraps_command_in_setsid():
    """正常执行的命令被 setsid 包裹，使后代归同一进程组可整组杀。"""
    provider = _make_provider()
    provider._run_cmd.return_value = (0, b"build done\n", b"")

    result = await provider._exec_in_container(
        "abc123", {"type": "command", "command": "cargo build --release", "timeout": 60}
    )

    assert result.success
    # 第一次调用是 exec 本身（后续可能有 cleanup 标记文件）；看第一次的 args
    first_call = provider._run_cmd.call_args_list[0]
    args = first_call.args[0]
    args_str = " ".join(args)
    assert "setsid" in args_str, f"命令未用 setsid 包裹: {args_str}"


# ---------------------------------------------------------------------------
# 2. 超时时整组杀（不只杀本地 docker client）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_kills_process_group_on_timeout(monkeypatch):
    """超时时：杀掉容器内整个进程组（防 cargo/rustc 后代变孤儿）。"""
    provider = _make_provider()

    kill_calls: list = []

    # _run_cmd 第一次（正常 exec）抛超时
    call_count = {"n": 0}

    async def fake_run(args, timeout=30):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 模拟超时（subprocess.TimeoutExpired）
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
        # 后续调用：cat(读PGID) 返回一个 PGID；其余记录
        kill_calls.append(args)
        if "cat" in args:
            return (0, b"7064\n", b"")  # 返回 PGID=7064
        return (0, b"", b"")

    provider._run_cmd = fake_run

    result = await provider._exec_in_container(
        "abc123", {"type": "command", "command": "cargo build", "timeout": 5}
    )

    assert result.success is False
    # 超时后必须发了整组杀命令（docker exec ... kill ... -PGID）
    assert len(kill_calls) > 0, "超时后未触发容器内整组杀"
    # kill_calls 包含 read_pgid(cat) + kill，找到含 kill 的那次
    kill_call = next((c for c in kill_calls if "kill" in c), None)
    assert kill_call is not None, f"应含 kill 整组杀命令，实际 {kill_calls}"
    # 含负 PGID（整组杀标志）
    has_neg_pgid = any(str(a).startswith("-") and a != "--" for a in kill_call)
    assert has_neg_pgid, f"应含负 PGID（整组杀），实际 {kill_call}"


# ---------------------------------------------------------------------------
# 3. TimeoutExpired 正确返回"超时"（非"执行命令失败"）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_timeout_returns_correct_error(monkeypatch):
    """subprocess.TimeoutExpired → 返回'超时'，而非'执行命令失败'。

    旧 except TimeoutError 接不住 subprocess.TimeoutExpired（继承 SubprocessError
    非 TimeoutError），落到 except Exception 报"执行命令失败"，错误信息误导。
    """
    provider = _make_provider()

    async def fake_run(args, timeout=30):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    provider._run_cmd = fake_run
    # 整组杀也走 _run_cmd，mock 后会再抛，但实现应吞掉 kill 的异常
    # 这里用 side_effect 控制：exec 抛超时，kill 成功
    call_count = {"n": 0}

    async def fake_run2(args, timeout=30):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
        return (0, b"", b"")

    provider._run_cmd = fake_run2

    result = await provider._exec_in_container(
        "abc123", {"type": "command", "command": "sleep 999", "timeout": 5}
    )

    assert result.success is False
    # 错误信息应含"超时"，不含"执行命令失败"
    assert result.error is not None
    assert "超时" in result.error, f"应报超时，实际: {result.error}"
    assert "执行命令失败" not in result.error, f"不应误报'执行命令失败': {result.error}"


# ---------------------------------------------------------------------------
# 4. 正常完成不受影响（不误触发 kill）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_normal_completion_no_kill():
    """命令正常完成时，不触发整组杀（kill）。"""
    provider = _make_provider()
    all_calls: list = []

    async def fake_run(args, timeout=30):
        all_calls.append(args)
        return (0, b"ok\n", b"")

    provider._run_cmd = fake_run

    result = await provider._exec_in_container(
        "abc123", {"type": "command", "command": "echo hello", "timeout": 10}
    )

    assert result.success
    # 正常完成：第一次是 exec（含 setsid），后续至多清理标记文件，但不应有 kill
    has_kill = any("kill" in c for c in all_calls)
    assert not has_kill, f"正常完成不应触发 kill，实际调用: {all_calls}"
