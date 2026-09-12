# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""看门狗生命周期 / 终止与输入失败分支 / WSL 直连 / 后端行为补测。

覆盖面（外部依赖全桩，不打真实进程/docker/psutil）：
- 看门狗判据链：无进程早退、单进程内存失控（idle 排序杀）、内存高水位
  按 idle 杀到回落即停、采样失败降级、孤儿兜底杀、backend kill 异常隔离、
  reap 强杀兜底、shutdown_all 停看门狗+终止计数；
- send_input / terminate_process 的参数与管道失败分支；
- WSL 命令解析与 Windows 路径转 WSL 路径（纯函数，parametrize 展开）；
- _spawn_local_process 的 shell 选择分支（create_subprocess_exec 桩）；
- Local/Container 两个后端的 kill/采样契约。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bash_types import ProcessInfo, WorkUnit
from process_manager import (
    ContainerProcessBackend,
    LocalProcessBackend,
    ProcessManager,
    _get_container_backend,
)

pytestmark = pytest.mark.unit


# ─────────────────────────── 桩对象 ───────────────────────────


class FakeBackend:
    """记录 kill/采样的假后端（外部依赖桩）。"""

    def __init__(
        self,
        *,
        memory_ratios: list[float | None] | None = None,
        unit_rss: dict[int, int] | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self.killed: list[tuple[int, bool]] = []
        self._memory_ratios = list(memory_ratios or [])
        self._unit_rss = dict(unit_rss or {})
        self._kill_error = kill_error

    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        if self._kill_error is not None:
            raise self._kill_error
        self.killed.append((unit.pid, force))

    async def sample_memory(self) -> float | None:
        if self._memory_ratios:
            return self._memory_ratios.pop(0)
        return None

    async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
        return self._unit_rss.get(unit.pid)


class FakeProcess:
    """假 asyncio 进程：可编程 wait 行为与 returncode。"""

    def __init__(
        self,
        pid: int = 4321,
        *,
        returncode: int | None = None,
        wait_delay: float | None = None,
        wait_forever: bool = False,
        kill_error: Exception | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self._wait_delay = wait_delay
        self._wait_forever = wait_forever
        self._kill_error = kill_error
        self.kill_called = 0

    async def wait(self) -> int:
        if self._wait_forever:
            await asyncio.sleep(1000)
        if self._wait_delay:
            await asyncio.sleep(self._wait_delay)
        return self.returncode or 0

    def kill(self) -> None:
        self.kill_called += 1
        if self._kill_error is not None:
            raise self._kill_error
        # 真实语义：kill 后进程可被回收，wait 不再挂起
        self._wait_forever = False


def _make_manager(tmp_path: Path, **kwargs: Any) -> ProcessManager:
    return ProcessManager(log_dir=tmp_path / "logs", **kwargs)


def _register(
    mgr: ProcessManager,
    pid: int,
    *,
    status: str = "running",
    process: Any = None,
    backend: Any = None,
    metadata: dict[str, Any] | None = None,
    last_access: float | None = None,
    output_task: Any = None,
) -> ProcessInfo:
    info = ProcessInfo(
        pid=pid,
        command=f"cmd-{pid}",
        start_time=time.time(),
        log_file=mgr.log_dir / f"bash_{pid}.log",
        process=process,
        status=status,
        backend=backend,
        last_access_time=last_access if last_access is not None else time.time(),
        metadata=metadata or {},
        output_task=output_task,
    )
    mgr.active_processes[pid] = info
    return info


# ─────────────────────────── 看门狗判据链 ───────────────────────────


async def test_watchdog_check_once_without_running_processes_returns(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    mgr._memory_backend = backend
    _register(mgr, 1, status="completed")

    await mgr._watchdog_check_once()

    assert backend.killed == []


async def test_watchdog_check_once_kills_orphan_beyond_timeout(tmp_path) -> None:
    """孤儿兜底：idle 超 orphan_timeout 无条件杀（内存水位无关）。"""
    mgr = _make_manager(tmp_path)
    backend = FakeBackend(memory_ratios=[0.1])  # 低水位，不触发水位判据
    mgr._memory_backend = backend
    proc = FakeProcess(pid=7, returncode=None)
    _register(
        mgr, 7, process=proc, backend=backend,
        last_access=time.time() - mgr._orphan_timeout - 1,
    )

    await mgr._watchdog_check_once()

    assert (7, True) in backend.killed
    assert mgr.active_processes[7].status == "terminated"


async def test_unit_memory_cleanup_kills_over_limit_by_idle_order(tmp_path) -> None:
    """单进程失控：RSS 超阈值的工作单元按 idle 降序杀；未超限不杀。"""
    mgr = _make_manager(tmp_path)
    mgr._unit_memory_limit = 100  # 100 字节阈值，便于构造超限
    now = time.time()
    backend = FakeBackend(unit_rss={11: 200, 12: 200, 13: 50})
    # 11 比 12 更久没访问 → 先杀 11
    _register(mgr, 11, backend=backend, last_access=now - 100)
    _register(mgr, 12, backend=backend, last_access=now - 10)
    _register(mgr, 13, backend=backend, last_access=now - 100)

    await mgr._cleanup_by_unit_memory(
        [(11, mgr.active_processes[11]), (12, mgr.active_processes[12]),
         (13, mgr.active_processes[13])],
        now,
    )

    assert [pid for pid, _ in backend.killed] == [11, 12]


async def test_unit_memory_cleanup_disabled_when_limit_nonpositive(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._unit_memory_limit = 0
    backend = FakeBackend(unit_rss={11: 10**9})
    info = _register(mgr, 11, backend=backend)

    await mgr._cleanup_by_unit_memory([(11, info)], time.time())

    assert backend.killed == []


async def test_unit_memory_cleanup_skips_backend_sample_error(tmp_path) -> None:
    """采样失败的工作单元跳过（best-effort），不误杀。"""

    class ErrBackend(FakeBackend):
        async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
            raise RuntimeError("psutil exploded")

    mgr = _make_manager(tmp_path)
    mgr._unit_memory_limit = 100
    backend = ErrBackend()
    info = _register(mgr, 11, backend=backend)

    await mgr._cleanup_by_unit_memory([(11, info)], time.time())

    assert backend.killed == []


async def test_idle_cleanup_stops_when_memory_falls_below_low_watermark(tmp_path) -> None:
    """水位清理：杀最闲一个后重采样已回落低水位 → 停，不追杀。"""
    mgr = _make_manager(tmp_path)
    # 第 0 次不采样（调用方已确认高）；杀掉第一个后采样回落 0.5 < 0.70
    backend = FakeBackend(memory_ratios=[0.5])
    mgr._memory_backend = backend
    now = time.time()
    infos = [
        (21, _register(mgr, 21, backend=backend, last_access=now - 50)),
        (22, _register(mgr, 22, backend=backend, last_access=now - 5)),
    ]

    await mgr._cleanup_by_idle(infos, now)

    assert [pid for pid, _ in backend.killed] == [21]


async def test_idle_cleanup_stops_on_sample_error(tmp_path) -> None:
    class ErrBackend(FakeBackend):
        async def sample_memory(self) -> float | None:
            raise RuntimeError("boom")

    mgr = _make_manager(tmp_path)
    backend = ErrBackend()
    mgr._memory_backend = backend
    now = time.time()
    infos = [
        (21, _register(mgr, 21, backend=backend, last_access=now - 50)),
        (22, _register(mgr, 22, backend=backend, last_access=now - 5)),
    ]

    await mgr._cleanup_by_idle(infos, now)

    # 第一个直接杀（高水位已确认），第二次采样抛错 → 停止，不再杀第二个
    assert [pid for pid, _ in backend.killed] == [21]


async def test_idle_cleanup_noop_without_memory_backend(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    info = _register(mgr, 31, backend=backend)

    await mgr._cleanup_by_idle([(31, info)], time.time())

    assert backend.killed == []


async def test_watchdog_check_once_rechecks_memory_sample_failure(tmp_path) -> None:
    """水位采样失败 → 跳过水位判据（孤儿兜底仍生效），不抛。"""
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    mgr._memory_backend = backend

    async def broken_sample() -> float | None:
        raise RuntimeError("sampling down")

    backend.sample_memory = broken_sample  # type: ignore[method-assign]
    proc = FakeProcess(pid=41)
    _register(mgr, 41, process=proc, backend=backend)  # idle≈0，不触发孤儿

    await mgr._watchdog_check_once()

    assert backend.killed == []
    assert mgr.active_processes[41].status == "running"


async def test_watchdog_kill_with_backend_reaps_and_marks_terminated(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    proc = FakeProcess(pid=51, returncode=0)
    info = _register(mgr, 51, process=proc, backend=backend, metadata={"container_pid": 999})

    await mgr._watchdog_kill(51, info, "unit_memory")

    # 容器 pid 优先于 host pid
    assert backend.killed == [(999, True)]
    assert info.status == "terminated"


async def test_watchdog_kill_backend_failure_is_contained(tmp_path) -> None:
    """backend.kill 抛异常 → 记日志不炸，状态不误标。"""
    mgr = _make_manager(tmp_path)
    backend = FakeBackend(kill_error=RuntimeError("docker down"))
    info = _register(mgr, 52, process=FakeProcess(pid=52), backend=backend)

    await mgr._watchdog_kill(52, info, "orphan")

    assert info.status == "running"


async def test_reap_after_kill_force_kills_when_wait_times_out(tmp_path) -> None:
    """backend 整树杀后 wait 卡死 → 3s 超时 → process.kill() 兜底。"""
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=61, wait_forever=True)
    info = _register(mgr, 61, process=proc)

    await asyncio.wait_for(mgr._reap_after_kill(61, info), timeout=5.0)

    assert proc.kill_called == 1
    assert info.status == "terminated"


async def test_reap_after_kill_waits_clean_exit(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=62, returncode=0)
    info = _register(mgr, 62, process=proc)

    await mgr._reap_after_kill(62, info)

    assert proc.kill_called == 0
    assert info.status == "terminated"


async def test_shutdown_all_terminates_only_running_and_counts(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    _register(mgr, 71, process=FakeProcess(pid=71), backend=backend)
    _register(mgr, 72, process=FakeProcess(pid=72), backend=backend)
    _register(mgr, 73, status="completed", process=FakeProcess(pid=73))

    killed = await mgr.shutdown_all()

    assert killed == 2
    assert mgr.active_processes[71].status == "terminated"
    assert mgr.active_processes[72].status == "terminated"
    assert mgr.active_processes[73].status == "completed"


async def test_shutdown_all_cancels_watchdog_task(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    started = asyncio.Event()

    async def _wd() -> None:
        started.set()
        await asyncio.sleep(1000)

    mgr._watchdog_task = asyncio.get_running_loop().create_task(_wd())
    await started.wait()

    killed = await mgr.shutdown_all()

    assert killed == 0
    assert mgr._watchdog_task is None
    assert mgr._watchdog_task is None or mgr._watchdog_task.cancelled()


def test_ensure_watchdog_without_running_loop_is_noop(tmp_path) -> None:
    """同步上下文（无事件循环）创建看门狗 → 静默跳过，不抛。"""
    mgr = _make_manager(tmp_path)

    mgr._ensure_watchdog()  # pytest sync 用例，无 running loop

    assert mgr._watchdog_task is None


# ─────────────────────────── 终止与输入失败分支 ───────────────────────────


async def test_terminate_process_missing_pid(tmp_path) -> None:
    mgr = _make_manager(tmp_path)

    ok, err = await mgr.terminate_process(12345)

    assert ok is False
    assert "不存在" in (err or "")


async def test_terminate_process_rejects_non_running(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    _register(mgr, 81, status="completed", process=FakeProcess(pid=81))

    ok, err = await mgr.terminate_process(81)

    assert ok is False
    assert err is not None


async def test_terminate_process_success_records_and_marks(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    info = _register(mgr, 82, process=FakeProcess(pid=82, returncode=0), backend=backend)
    info.log_file.write_text("# Bash Command Log\n", encoding="utf-8")

    ok, err = await mgr.terminate_process(82, force=True)

    assert (ok, err) == (True, None)
    assert info.status == "terminated"
    assert backend.killed == [(82, True)]
    assert "terminated by user" in info.log_file.read_text(encoding="utf-8")


async def test_terminate_process_swallows_process_lookup(tmp_path) -> None:
    """进程已消失（ProcessLookupError）→ 视为已终止成功。"""
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=83)
    proc.wait = lambda: (_ for _ in ()).throw(ProcessLookupError())  # type: ignore[method-assign]
    info = _register(mgr, 83, process=proc)

    ok, err = await mgr.terminate_process(83)

    assert (ok, err) == (True, None)
    assert info.status == "terminated"


async def test_terminate_process_reports_backend_failure(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    backend = FakeBackend(kill_error=RuntimeError("access denied"))
    info = _register(mgr, 84, process=FakeProcess(pid=84), backend=backend)

    ok, err = await mgr.terminate_process(84)

    assert ok is False
    assert "access denied" in (err or "")


def _fake_stdin(error: Exception | None = None) -> Any:
    stdin = SimpleNamespace()

    def write(data: bytes) -> None:
        if error is not None:
            raise error

    async def drain() -> None:
        return None

    stdin.write = write  # type: ignore[attr-defined]
    stdin.drain = drain  # type: ignore[attr-defined]
    return stdin


async def test_send_input_reject_each_guard_branch(tmp_path) -> None:
    """入口守卫逐支：pid 缺失 / 非 running / 进程对象缺失。"""
    mgr = _make_manager(tmp_path)
    _register(mgr, 91, status="completed", process=FakeProcess(pid=91))
    _register(mgr, 92, process=None)

    assert (await mgr.send_input(99, "hi"))[0] is False
    assert "无法接受输入" in (await mgr.send_input(91, "hi"))[1]
    assert "不可用" in (await mgr.send_input(92, "hi"))[1]


async def test_send_input_rejects_closed_stdin(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=93)
    proc.stdin = None
    _register(mgr, 93, process=proc)

    ok, err = await mgr.send_input(93, "hi")

    assert ok is False
    assert "标准输入已关闭" in (err or "")


async def test_send_input_reports_broken_pipe(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=94)
    proc.stdin = _fake_stdin(error=BrokenPipeError("pipe closed"))
    info = _register(mgr, 94, process=proc)

    ok, err = await mgr.send_input(94, "hi")

    assert ok is False
    assert "管道已断开" in (err or "")
    assert info.last_access_time > 0


async def test_send_input_success_writes_masked_log(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=95)
    proc.stdin = _fake_stdin()
    info = _register(mgr, 95, process=proc)
    info.log_file.write_text("", encoding="utf-8")

    ok, err = await mgr.send_input(95, "echo hello")

    assert (ok, err) == (True, None)
    assert "[INPUT]" in info.log_file.read_text(encoding="utf-8")


# ─────────────────────────── 读面与降级 ───────────────────────────


def test_read_log_lines_missing_file_vs_io_error(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    assert mgr._read_log_lines(tmp_path / "absent.log") == []
    # 目录当文件读 → OSError → None（与"无日志"显式区分）
    d = tmp_path / "as_dir"
    d.mkdir()
    assert mgr._read_log_lines(d) is None


def test_read_tail_lines_filters_comments_and_caps_window(tmp_path) -> None:
    log = tmp_path / "bash_1.log"
    log.write_text(
        "# header\n" + "".join(f"line{i}\n" for i in range(10)), encoding="utf-8"
    )
    # 文件以 \n 结尾：split 尾部产生空串占位，窗口内有效行 = max_lines-1
    assert ProcessManager._read_tail_lines(log, max_lines=4) == ["line7", "line8", "line9"]
    # 无尾换行：残留半行完整保留在窗口内
    log2 = tmp_path / "bash_1b.log"
    log2.write_text("# header\nline0\nline1", encoding="utf-8")
    assert ProcessManager._read_tail_lines(log2) == ["line0", "line1"]
    empty = tmp_path / "bash_2.log"
    empty.write_text("", encoding="utf-8")
    assert ProcessManager._read_tail_lines(empty) == []
    assert ProcessManager._read_tail_lines(tmp_path / "absent.log") == []


def test_mask_secrets_empty_text_short_circuit() -> None:
    assert ProcessManager._mask_secrets("") == ""


def test_sync_poll_process_reflects_returncode(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    running = _register(mgr, 101, process=FakeProcess(pid=101, returncode=None))
    mgr._sync_poll_process(running)
    assert running.status == "running"

    ok = _register(mgr, 102, process=FakeProcess(pid=102, returncode=0))
    mgr._sync_poll_process(ok)
    assert ok.status == "completed" and ok.exit_code == 0

    bad = _register(mgr, 103, process=FakeProcess(pid=103, returncode=2))
    mgr._sync_poll_process(bad)
    assert bad.status == "error" and bad.exit_code == 2

    nopro = _register(mgr, 104, process=None)
    mgr._sync_poll_process(nopro)
    assert nopro.status == "running"


def test_cleanup_finished_removes_only_terminal_states(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    _register(mgr, 111, status="completed")
    _register(mgr, 112, status="terminated")
    _register(mgr, 113, status="running")

    mgr.cleanup_finished()

    assert set(mgr.active_processes) == {113}


def test_touch_access_updates_timestamp(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    info = _register(mgr, 121, last_access=0.0)

    mgr._touch_access(121)

    assert info.last_access_time > 0
    mgr._touch_access(404)  # 不存在的 pid 不抛


def test_read_log_by_pid_parses_header_owner_and_exit_code(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    log = mgr.log_dir / "bash_131.log"
    log.write_text(
        "# Bash Command Log\n"
        "# Command: cargo build\n"
        "# Owner: sess-a\n"
        "compiling v1\n"
        "# Process ended with exit code: 3\n",
        encoding="utf-8",
    )

    data = mgr.read_log_by_pid(131)

    assert data is not None
    assert data["command"] == "cargo build"
    assert data["owner"] == "sess-a"
    assert data["exit_code"] == 3
    assert "compiling v1" in data["output"]


def test_read_log_by_pid_missing_returns_none(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    assert mgr.read_log_by_pid(999) is None


def test_summary_from_disk_composes_degraded_summary(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    log = mgr.log_dir / "bash_141.log"
    log.write_text(
        "# Command: pytest\n"
        "1 passed\n"
        "# Process ended with exit code: 0\n",
        encoding="utf-8",
    )

    summary = mgr._summary_from_disk(141)

    assert summary is not None
    assert summary["status"] == "completed"
    assert summary["exit_code"] == 0
    assert summary["pid"] == 141
    assert mgr._summary_from_disk(999) is None


async def test_wait_output_settled_awaits_pending_task(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    done = asyncio.Event()

    async def _job() -> None:
        await asyncio.sleep(0.05)
        done.set()

    task = asyncio.get_running_loop().create_task(_job())
    _register(mgr, 151, output_task=task)

    await mgr.wait_output_settled(151, timeout=2.0)

    assert done.is_set()


async def test_wait_output_settled_noop_for_missing_or_done(tmp_path) -> None:
    mgr = _make_manager(tmp_path)
    await mgr.wait_output_settled(404)  # pid 不存在不抛

    done_task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
    _register(mgr, 152, output_task=done_task)
    await done_task
    await mgr.wait_output_settled(152)


async def test_read_container_pid_falls_back_to_host_pid() -> None:
    stdout = SimpleNamespace(
        readline=lambda: asyncio.sleep(0, result=b"1234\n")
    )
    proc = SimpleNamespace(pid=777, stdout=stdout)
    assert await ProcessManager._read_container_pid(proc) == 1234

    bad_stdout = SimpleNamespace(readline=lambda: asyncio.sleep(0, result=b"not-a-pid\n"))
    proc2 = SimpleNamespace(pid=778, stdout=bad_stdout)
    assert await ProcessManager._read_container_pid(proc2) == 778


# ─────────────────────────── 复合命令判定（引号状态机） ───────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo hi", False),
        ("echo a && echo b", True),
        ("echo a; echo b", True),
        ("echo a | wc -l", True),
        ('echo "a;b|c"', False),  # 引号内元字符不算复合
        ("echo 'a&&b'", False),
        ('echo "a$(whoami)"', True),  # 双引号内命令替换算复合
        ("echo `whoami`", True),
        ("echo a \\; b", False),  # 转义分号
        ("for i in 1; do echo $i; done", True),
        ("echo $HOME", False),
    ],
)
def test_is_compound_command(command: str, expected: bool) -> None:
    assert ProcessManager._is_compound_command(command) is expected


# ─────────────────────────── WSL 参数解析与路径转换 ───────────────────────────


def test_is_wsl_command_shape() -> None:
    assert ProcessManager._WSL_COMMAND_RE.match("wsl ls")
    assert ProcessManager._WSL_COMMAND_RE.match("WSL.EXE -d U")
    assert not ProcessManager._WSL_COMMAND_RE.match("wslx ls")
    assert not ProcessManager._WSL_COMMAND_RE.match("echo wsl")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("wsl", ["wsl"]),
        ("wsl   ", ["wsl"]),
        # -d 带值 → wsl_opts；命令部分套 bash -c
        (
            "wsl -d Ubuntu-20.04 ls -la",
            ["wsl", "-d", "Ubuntu-20.04", "-e", "bash", "-c", "ls -la"],
        ),
        # 普通命令：bash -c 包装
        ("wsl echo $HOME", ["wsl", "-e", "bash", "-c", "echo $HOME"]),
        # 只有标志没有命令 → 只保留标志
        ("wsl -d Ubuntu", ["wsl", "-d", "Ubuntu"]),
        # shlex 解析失败（不配对引号）→ 整串交 bash -c
        ('wsl echo "unclosed', ["wsl", "-e", "bash", "-c", 'echo "unclosed']),
    ],
)
def test_parse_wsl_args(command: str, expected: list[str]) -> None:
    assert ProcessManager._parse_wsl_args(command) == expected


def test_join_for_bash_c() -> None:
    assert ProcessManager._join_for_bash_c(["echo", "a b"]) == "echo 'a b'"
    assert ProcessManager._join_for_bash_c(["echo", "$HOME|wc"]) == "echo $HOME|wc"


def test_convert_windows_paths_non_windows_is_noop(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    assert ProcessManager._convert_windows_paths_for_wsl("cat D:\\a\\b") == "cat D:\\a\\b"


@pytest.mark.skipif(sys.platform != "win32", reason="转换仅在 Windows 生效")
def test_convert_windows_paths_on_windows() -> None:
    conv = ProcessManager._convert_windows_paths_for_wsl
    assert conv("cat D:\\a\\b") == "cat /mnt/d/a/b"
    assert conv("cd D:/x/y && ls") == "cd /mnt/d/x/y && ls"
    # 长路径前缀剥离
    assert conv("cat \\\\?\\D:\\long\\path") == "cat /mnt/d/long/path"
    # WSL UNC → 内部路径
    assert conv("ls \\\\wsl$\\Ubuntu\\home\\me") == "ls /home/me"
    assert conv("ls \\\\wsl.localhost\\Ubuntu\\srv") == "ls /srv"
    # URL 不误伤（盘符负向回查）
    url = "curl -L https://sh.rustup.rs | sh"
    assert conv(url) == url
    # 非路径 token 原样
    assert conv("echo done") == "echo done"


def test_convert_single_windows_path_wsl_unc_short_form() -> None:
    # share 之后不足 5 段 → 原样返回
    assert (
        ProcessManager._convert_single_windows_path("\\\\wsl$\\Ubuntu")
        == "\\\\wsl$\\Ubuntu"
    )
    # 非路径原样
    assert ProcessManager._convert_single_windows_path("justword") == "justword"


# ─────────────────────────── _spawn_local_process 分支选择 ───────────────────────────


class _SpawnRecorder:
    """捕获 create_subprocess_exec/shell 调用参数，返回哨兵进程。"""

    def __init__(self) -> None:
        self.exec_calls: list[tuple[Any, ...]] = []
        self.shell_calls: list[str] = []

    async def exec(self, *args: Any, **kwargs: Any) -> Any:
        self.exec_calls.append(args)
        return SimpleNamespace(pid=1)

    async def shell(self, cmd: str, **kwargs: Any) -> Any:
        self.shell_calls.append(cmd)
        return SimpleNamespace(pid=2)


@pytest.fixture
def spawn_recorder(monkeypatch) -> _SpawnRecorder:
    rec = _SpawnRecorder()
    import process_manager as pm

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", rec.exec)
    monkeypatch.setattr(pm.asyncio, "create_subprocess_shell", rec.shell)
    return rec


@pytest.mark.asyncio
async def test_spawn_local_wsl_direct_priority(tmp_path, monkeypatch, spawn_recorder) -> None:
    """WSL 可用 + wsl 命令 → 直连分支（不走 bash -c 包装）。"""
    monkeypatch.setattr("shutil.which", lambda name: "C:\\wsl.exe" if name == "wsl" else None)
    import process_manager as pm

    monkeypatch.setattr(pm.platform, "system", lambda: "Windows")
    mgr = _make_manager(tmp_path)

    await mgr._spawn_local_process("wsl ls -la", None, {}, is_windows=True)

    assert spawn_recorder.exec_calls == [
        ("wsl", "-e", "bash", "-c", "ls -la")
    ] or spawn_recorder.exec_calls[0][0] == "wsl"


@pytest.mark.asyncio
async def test_spawn_local_msys_bash_wraps_compound_with_pipefail(
    tmp_path, monkeypatch, spawn_recorder
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "C:\\bash.exe" if name == "bash" else None)
    mgr = _make_manager(tmp_path)

    await mgr._spawn_local_process("echo a && echo b", None, {}, is_windows=True)

    args = spawn_recorder.exec_calls[0]
    assert args[:3] == ("bash", "-c", "set -o pipefail; echo a && echo b")


@pytest.mark.asyncio
async def test_spawn_local_windows_cmd_fallback(tmp_path, monkeypatch, spawn_recorder) -> None:
    """无 bash/wsl → cmd /c 兜底。"""
    monkeypatch.setattr("shutil.which", lambda name: None)
    mgr = _make_manager(tmp_path)

    await mgr._spawn_local_process("dir", None, {}, is_windows=True)

    assert spawn_recorder.shell_calls and "cmd /c" in spawn_recorder.shell_calls[0]


@pytest.mark.asyncio
async def test_spawn_local_posix_compound_uses_bash(tmp_path, monkeypatch, spawn_recorder) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/bash" if name == "bash" else None)
    mgr = _make_manager(tmp_path)

    await mgr._spawn_local_process("echo a | wc -l", None, {}, is_windows=False)

    args = spawn_recorder.exec_calls[0]
    assert args[:3] == ("bash", "-c", "set -o pipefail; echo a | wc -l")


@pytest.mark.asyncio
async def test_spawn_local_posix_simple_uses_shell(tmp_path, monkeypatch, spawn_recorder) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    mgr = _make_manager(tmp_path)

    await mgr._spawn_local_process("ls", None, {}, is_windows=False)

    assert spawn_recorder.shell_calls == ["ls"]


@pytest.mark.asyncio
async def test_start_wsl_process_injects_lang(tmp_path, monkeypatch, spawn_recorder) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "wsl.exe")
    mgr = _make_manager(tmp_path)
    env = {"PATH": "x"}

    await mgr._start_wsl_process("wsl echo hi", None, env)

    assert env["LANG"] == "en_US.UTF-8"
    args = spawn_recorder.exec_calls[0]
    assert args[0] == "wsl" and "bash" in args


# ─────────────────────────── 后端契约 ───────────────────────────


@pytest.mark.asyncio
async def test_container_backend_kill_uses_sh_builtin_and_metadata_priority() -> None:
    backend = ContainerProcessBackend("cid-default")
    calls: list[tuple[list[str], float]] = []

    async def fake_run_cmd(args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        calls.append((args, timeout))
        return 0, b"", b""

    backend._run_cmd = fake_run_cmd  # type: ignore[method-assign]

    # metadata['container_id'] 优先于实例 id
    unit = WorkUnit(pid=7, command="x", metadata={"container_id": "cid-meta"})
    await backend.kill(unit, force=True)
    args, timeout = calls[0]
    assert args[:5] == ["docker", "exec", "cid-meta", "sh", "-c"]
    assert "kill -9 7" in args[5]
    assert timeout == 10

    # 无 metadata → 用实例 id；非 force → -15
    await backend.kill(WorkUnit(pid=8, command="x"), force=False)
    assert "cid-default" in calls[1][0][2]
    assert "kill -15 8" in calls[1][0][5]


@pytest.mark.asyncio
async def test_container_backend_kill_missing_container_id_raises() -> None:
    backend = ContainerProcessBackend("")
    with pytest.raises(KeyError):
        await backend.kill(WorkUnit(pid=9, command="x"))


@pytest.mark.asyncio
async def test_container_backend_kill_swallows_run_cmd_error() -> None:
    backend = ContainerProcessBackend("cid")

    async def broken(*a: Any, **k: Any) -> tuple[int, bytes, bytes]:
        raise RuntimeError("docker daemon down")

    backend._run_cmd = broken  # type: ignore[method-assign]
    await backend.kill(WorkUnit(pid=10, command="x"))  # 不抛


@pytest.mark.asyncio
async def test_container_backend_sampling_returns_none() -> None:
    backend = ContainerProcessBackend("cid")
    assert await backend.sample_memory() is None
    assert await backend.sample_unit_memory(WorkUnit(pid=1, command="x")) is None


def test_get_container_backend_is_cached_per_container() -> None:
    a = _get_container_backend("c1")
    b = _get_container_backend("c1")
    c = _get_container_backend("c2")
    assert a is b
    assert a is not c


@pytest.mark.asyncio
async def test_local_backend_sample_memory_returns_ratio() -> None:
    backend = LocalProcessBackend()
    ratio = await backend.sample_memory()
    assert ratio is None or 0.0 <= ratio <= 1.0


def test_local_backend_kill_tree_no_such_process_is_noop(monkeypatch) -> None:
    """psutil 报 NoSuchProcess → 静默返回（进程已消失属正常）。"""
    import psutil as real_psutil

    backend = LocalProcessBackend()

    def fake_process(pid: int) -> Any:
        raise real_psutil.NoSuchProcess(pid)

    monkeypatch.setattr(real_psutil, "Process", fake_process)
    backend._kill_tree_sync(2**22, force=True)  # 不抛


def test_local_backend_kill_tree_kills_descendants_then_root(monkeypatch) -> None:
    """杀树顺序：后代叶子先杀、根最后；AccessDenied 逐个跳过。"""
    psutil_stub = SimpleNamespace(
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
        Process=None,
    )
    killed_order: list[str] = []

    class FakePsutilProc:
        def __init__(self, name: str, *, deny: bool = False) -> None:
            self._name = name
            self._deny = deny

        def children(self, recursive: bool = True) -> list["FakePsutilProc"]:
            return [FakePsutilProc("leaf1"), FakePsutilProc("denied", deny=True)]

        def kill(self) -> None:
            if self._deny:
                raise psutil_stub.AccessDenied()
            killed_order.append(self._name)

        def terminate(self) -> None:
            killed_order.append(self._name + ":term")

    psutil_stub.Process = lambda pid: FakePsutilProc("root")
    monkeypatch.setitem(sys.modules, "psutil", psutil_stub)

    LocalProcessBackend._kill_tree_sync(2**21, force=True)

    assert killed_order == ["leaf1", "root"]
