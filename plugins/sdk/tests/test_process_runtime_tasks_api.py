# @feature: FP-0.2.一 EnvProcessRuntime Tasks 状态机 API | @ci: python-coverage
"""EnvProcessRuntime Tasks 状态机 API 测试。

覆盖（对齐官方 MCP Tasks，SEP-1686 Final）：
- from_inputs：无 _container_id → None（非隔离上下文，调用方自定宿主分支）；
  有 → 三后端上下文落实例；
- 五态映射：running→working / completed rc=0→completed / rc!=0→failed /
  terminated→cancelled；
- 终态不可逆：cancel 后 status 恒 cancelled，再 cancel 幂等忽略；
- result 仅 completed 可取（SEP §4.6），否则 TaskStateError；
- 未知任务（无登记无日志）→ TaskStateError（tasks/get 查无此 id 同形）；
- 磁盘降级态：进程被即时清理后凭日志尾 exit_code 分态；
- start 委托：句柄立即返回、container_id/exec_backend 覆盖传递、超时语义
  透传（timeout 不进 manager 调用面——manager.start_process 的 timeout 是
  命令语义一部分）。

实现载体 ProcessManager 的行为由 bash 车道全量覆盖；本文件只钉词汇映射与
门面委托（mock 仅限 ProcessManager 外层委托路径）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agentos_plugin_sdk.process_runtime import (
    TERMINAL_TASK_STATUSES,
    EnvProcessRuntime,
    ProcessInfo,
    TaskStateError,
    TaskStatus,
)

pytestmark = pytest.mark.unit


def _make_runtime(tmp_path: Path, **kwargs: object) -> EnvProcessRuntime:
    from agentos_plugin_sdk.process_runtime import ProcessManager

    return EnvProcessRuntime(ProcessManager(log_dir=tmp_path / "logs"), **kwargs)  # type: ignore[arg-type]


def _inject(
    runtime: EnvProcessRuntime,
    pid: int,
    status: str,
    exit_code: int | None = None,
) -> None:
    """合成注入进程条目（process=None：_sync_poll_process 早退，安全）。"""
    manager = runtime._manager
    manager.active_processes[pid] = ProcessInfo(
        pid=pid,
        command="synthetic",
        start_time=0.0,
        log_file=manager.log_dir / f"bash_{pid}.log",
        process=None,
        status=status,
        exit_code=exit_code,
    )


class TestFromInputs:
    def test_无container_id返回None(self, tmp_path: Path) -> None:
        """非隔离上下文（guard 未注入）→ None，调用方自定宿主分支。"""
        assert EnvProcessRuntime.from_inputs({"command": "ls"}) is None
        assert EnvProcessRuntime.from_inputs({}) is None

    def test_有container_id落实例字段(self, tmp_path: Path) -> None:
        backend = {"backend": "wsl_native", "distro": "Ubuntu"}
        rt = EnvProcessRuntime.from_inputs({"_container_id": "cua-ws1", "_exec_backend": backend})
        assert rt is not None
        assert rt._container_id == "cua-ws1"
        assert rt._exec_backend == backend

    def test_exec_backend非dict降级None(self) -> None:
        rt = EnvProcessRuntime.from_inputs({"_container_id": "c1", "_exec_backend": "forged"})
        assert rt is not None
        assert rt._exec_backend is None


class TestStatusMapping:
    def test_running映射working(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 101, "running")
        assert rt.status(101) is TaskStatus.WORKING

    def test_completed按退出码分态(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 102, "completed", exit_code=0)
        _inject(rt, 103, "completed", exit_code=2)
        assert rt.status(102) is TaskStatus.COMPLETED
        assert rt.status(103) is TaskStatus.FAILED

    def test_error态按退出码分态(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 104, "error", exit_code=137)
        assert rt.status(104) is TaskStatus.FAILED

    def test_terminated映射cancelled(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 105, "terminated")
        assert rt.status(105) is TaskStatus.CANCELLED

    def test_未知任务抛TaskStateError(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        with pytest.raises(TaskStateError):
            rt.status(999999)


class TestDiskDegradedStatus:
    def test_清理后凭日志exit_code分态(self, tmp_path: Path) -> None:
        """进程被即时清理（登记已清）：磁盘日志尾部 exit_code 决定终态。"""
        rt = _make_runtime(tmp_path)
        log = tmp_path / "logs" / "bash_201.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "# Bash Command Log\n# Command: echo ok\n\nok\n# Process ended with exit code: 0\n",
            encoding="utf-8",
        )
        assert rt.status(201) is TaskStatus.COMPLETED

        log.write_text(
            "# Bash Command Log\n# Command: false\n\nboom\n# Process ended with exit code: 1\n",
            encoding="utf-8",
        )
        assert rt.status(201) is TaskStatus.FAILED

        log.write_text("# Bash Command Log\n# Command: x\n\npartial\n", encoding="utf-8")
        assert rt.status(201) is TaskStatus.CANCELLED


class TestResult:
    @pytest.mark.asyncio
    async def test_非completed拒绝取结果(self, tmp_path: Path) -> None:
        """SEP-1686 §4.6：result 仅 completed 可取，否则报错。"""
        rt = _make_runtime(tmp_path)
        _inject(rt, 301, "running")
        with pytest.raises(TaskStateError) as exc_info:
            await rt.result(301)
        assert exc_info.value.status is TaskStatus.WORKING

    @pytest.mark.asyncio
    async def test_completed返回结果三元组(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 302, "completed", exit_code=0)
        # wait_output_settled 走真身（无 output_task 立即放行）；summary/output
        # 读缺失日志文件返回空——本用例钉的是状态门 + 返回形状
        data = await rt.result(302)
        assert set(data) == {"summary", "output", "exit_code"}
        assert data["exit_code"] == 0


class TestCancel:
    @pytest.mark.asyncio
    async def test_运行中取消落cancelled终态(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 401, "running")

        async def _terminate(pid: int, force: bool = False) -> tuple[bool, str | None]:
            # 复现真实契约：terminate_process 成功即置 terminated
            rt._manager.active_processes[pid].status = "terminated"
            return (True, None)

        rt._manager.terminate_process = AsyncMock(side_effect=_terminate)  # type: ignore[method-assign]
        ok, err = await rt.cancel(401)
        assert (ok, err) == (True, None)
        rt._manager.terminate_process.assert_awaited_once_with(401, force=True)
        assert rt.status(401) is TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_终态后取消幂等忽略(self, tmp_path: Path) -> None:
        """SEP-1686 §4.8.3：终态后取消通知被忽略。"""
        rt = _make_runtime(tmp_path)
        _inject(rt, 402, "completed", exit_code=0)
        ok, err = await rt.cancel(402)
        assert ok is False
        assert err is not None and "已终态" in err

    @pytest.mark.asyncio
    async def test_未知任务取消报不存在(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        ok, err = await rt.cancel(999999)
        assert ok is False
        assert err is not None and "不存在" in err


class TestStartDelegation:
    @pytest.mark.asyncio
    async def test_句柄立即返回且上下文透传(self, tmp_path: Path) -> None:
        """start 即 call-now：返回句柄 pid 不阻塞；构造的隔离上下文透传 manager。"""
        rt = _make_runtime(tmp_path, container_id="cua-ws1", exec_backend={"backend": "wsl_native"})
        rt._manager.start_process = AsyncMock(return_value=(555, tmp_path / "l.log"))  # type: ignore[method-assign]
        pid = await rt.start("cargo build", working_dir="/workspace", timeout=60)
        assert pid == 555
        rt._manager.start_process.assert_awaited_once_with(
            "cargo build",
            working_dir="/workspace",
            env=None,
            container_id="cua-ws1",
            owner=None,
            on_output=None,
            exec_backend={"backend": "wsl_native"},
        )

    @pytest.mark.asyncio
    async def test_显式参数覆盖构造上下文(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path, container_id="cua-default", exec_backend=None)
        rt._manager.start_process = AsyncMock(return_value=(556, tmp_path / "l.log"))  # type: ignore[method-assign]
        await rt.start(
            "ls",
            container_id="cua-other",
            exec_backend={"backend": "wsl_native"},
        )
        call = rt._manager.start_process.await_args
        assert call.kwargs["container_id"] == "cua-other"
        assert call.kwargs["exec_backend"] == {"backend": "wsl_native"}

    @pytest.mark.asyncio
    async def test_无容器上下文走本地通路(self, tmp_path: Path) -> None:
        """直构造（无 container_id）→ container_id=None，exec_backend 不透传。"""
        rt = _make_runtime(tmp_path)
        rt._manager.start_process = AsyncMock(return_value=(557, tmp_path / "l.log"))  # type: ignore[method-assign]
        await rt.start("echo hi")
        call = rt._manager.start_process.await_args
        assert call.kwargs["container_id"] is None
        assert call.kwargs["exec_backend"] is None


class TestTerminalIrreversibility:
    def test_终态集完备且含cancelled(self) -> None:
        """SEP-1686 §4.3：completed/failed/cancelled 为终态。"""
        assert TERMINAL_TASK_STATUSES == frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

    @pytest.mark.asyncio
    async def test_取消后状态恒cancelled(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 501, "running")

        async def _terminate(pid: int, force: bool = False) -> tuple[bool, str | None]:
            rt._manager.active_processes[pid].status = "terminated"
            return (True, None)

        rt._manager.terminate_process = AsyncMock(side_effect=_terminate)  # type: ignore[method-assign]
        await rt.cancel(501)
        for _ in range(3):
            assert rt.status(501) is TaskStatus.CANCELLED


class TestWait:
    @pytest.mark.asyncio
    async def test_已完成立即返回(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 601, "completed", exit_code=0)
        assert await rt.wait(601, timeout=1) is TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_超时返回当前状态不杀进程(self, tmp_path: Path) -> None:
        rt = _make_runtime(tmp_path)
        _inject(rt, 602, "running")
        st = await rt.wait(602, timeout=0.1)
        assert st is TaskStatus.WORKING
        # 句柄照常可用（超时不杀、不销毁句柄语义）
        assert rt.status(602) is TaskStatus.WORKING
        await asyncio.sleep(0)  # 事件循环让点（占位，防 lint）
