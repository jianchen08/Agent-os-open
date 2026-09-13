# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""bash 工具分支补测：LogCompressor 全行为面 + ProcessManager 容错/降级分支。

覆盖缺口（2026-09-13 实测 711/804、86/134）补齐：
- LogCompressor：内容型输出类型检测、进度提取各模式、警告/错误计数、
  最新消息截断、compress_errors 去重/封顶/归一化合并、_normalize_error、
  compress 的阈值/错误列表配置分支；
- ProcessManager：日志目录/清扫/写入的 OSError 容错分支、容器复合命令包装
  与 owner metadata、WSL bash LANG 注入、复合命令引号状态机补充用例、
  on_output 回调异常隔离（真实子进程）、流读取故障收尾、send_input 校验
  分支、terminate 优雅等待超时强杀、看门狗主循环/全退出早退、
  shutdown_all 失败分支、read_log_by_pid 坏退出码解析、_cleanup_if_needed
  惰性清理、LocalProcessBackend psutil 异常面、ContainerProcessBackend
  ._run_cmd 真实子进程。

打桩边界：docker exec 与本地 spawn 捕获参数（外部进程）；psutil 以 stub
模块注入（外部库）；时钟类等待以注入失败的 wait_for 等价模拟。真实子进程
只用于 on_output 回调链路与 _run_cmd（短命 python -c）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bash_types import LogCompressorConfig, OutputType, ProcessInfo, WorkUnit
from log_compressor import LogCompressor
from process_manager import (
    ContainerProcessBackend,
    LocalProcessBackend,
    ProcessManager,
)

pytestmark = pytest.mark.unit


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


class FakeBackend:
    """记录 kill/采样的假后端（外部依赖桩）。"""

    def __init__(
        self,
        *,
        memory_ratios: list[float | None] | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self.killed: list[tuple[int, bool]] = []
        self._memory_ratios = list(memory_ratios or [])
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
        return None


class FakeProcess:
    """假 asyncio 进程：可编程 wait 行为与 returncode。"""

    def __init__(
        self,
        pid: int = 4321,
        *,
        returncode: int | None = None,
        wait_forever: bool = False,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self._wait_forever = wait_forever
        self.kill_called = 0

    async def wait(self) -> int:
        if self._wait_forever:
            await asyncio.sleep(1000)
        return self.returncode or 0

    def kill(self) -> None:
        self.kill_called += 1
        self._wait_forever = False


# ═════════════════════════ LogCompressor ═════════════════════════


@pytest.mark.parametrize(
    ("command", "lines", "expected"),
    [
        ("git push origin main", [], OutputType.GIT),
        ("cargo build --release", [], OutputType.COMPILATION),
        ("pytest -q tests/", [], OutputType.PYTEST),
        ("docker build -t app .", [], OutputType.DOCKER_BUILD),
        ("", ["installing npm package from registry"], OutputType.NPM_INSTALL),
        ("", ["run pip install numpy"], OutputType.PIP_INSTALL),
        ("", ["Step 1/4 : docker build layer"], OutputType.DOCKER_BUILD),
        ("", ["= test session starts ="], OutputType.PYTEST),
        ("", ["   Compiling serde v1.0"], OutputType.COMPILATION),
        ("", ["nothing special here"], OutputType.GENERAL),
    ],
)
def test_detect_output_type(command: str, lines: list[str], expected: OutputType) -> None:
    assert LogCompressor().detect_output_type(command, lines) is expected


@pytest.mark.parametrize(
    ("lines", "output_type", "expected"),
    [
        (["added 15 packages in 2s"], OutputType.NPM_INSTALL, "15 packages"),
        (["Collecting numpy"], OutputType.PIP_INSTALL, "Collecting"),
        # 取最新一行：Collecting 在前、Installing 在后 → 返回 Installing
        (
            ["Collecting a", "Installing collected packages"],
            OutputType.PIP_INSTALL,
            "Installing",
        ),
        (["3 passed, 1 failed in 0.5s"], OutputType.PYTEST, "3 passed"),
        (["processing 42/100 items"], OutputType.GENERAL, "42/100"),
        (["45% complete"], OutputType.GENERAL, "45%"),
        (["[build 88%] done"], OutputType.GENERAL, "88%"),
        (["3 of 5 tests ran"], OutputType.GENERAL, "3/5"),
        (["all systems nominal"], OutputType.GENERAL, None),
    ],
)
def test_extract_progress(
    lines: list[str], output_type: OutputType, expected: str | None
) -> None:
    assert LogCompressor().extract_progress(lines, output_type) == expected


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([], (0, 0)),
        (["all good"], (0, 0)),
        (["warning: deprecated api"], (1, 0)),
        (["Warning: x", "WARN y"], (2, 0)),
        (["error: boom", "Traceback (most recent call last):"], (0, 2)),
        (["Killed"], (0, 1)),
        # 同一行同时命中警告与错误 → 两个计数各加一
        (["error: warning here"], (1, 1)),
    ],
)
def test_count_warnings_errors(lines: list[str], expected: tuple[int, int]) -> None:
    assert LogCompressor().count_warnings_errors(lines) == expected


def test_get_latest_message_skips_noise_and_returns_latest() -> None:
    comp = LogCompressor()
    lines = ["the actual latest message", "> interactive prompt", "ab", "   "]
    assert comp.get_latest_message(lines) == "the actual latest message"


def test_get_latest_message_truncates_over_long_line() -> None:
    result = LogCompressor().get_latest_message(["x" * 150], max_length=100)
    assert result == "x" * 100 + "..."


def test_get_latest_message_empty_when_all_filtered() -> None:
    assert LogCompressor().get_latest_message(["", "  ", "> ", "ok"]) == ""


def test_compress_errors_dedup_and_raw_modes() -> None:
    comp = LogCompressor()
    lines = ["error: file not found", "  error: file not found", "error: timeout"]
    assert comp.compress_errors(lines, dedup=True) == [
        "error: file not found (x2)",
        "error: timeout",
    ]
    assert comp.compress_errors(lines, dedup=False) == [
        "error: file not found",
        "error: file not found",
        "error: timeout",
    ]


def test_compress_errors_empty_input_returns_empty() -> None:
    assert LogCompressor().compress_errors(["fine", "also fine"]) == []


def test_compress_errors_merges_after_normalization() -> None:
    comp = LogCompressor()
    lines = [
        "2026-01-01 10:00:00 error: connect failed at svc.py:42",
        "2027-02-02 11:11:11 error: connect failed at svc.py:99",
    ]
    assert comp.compress_errors(lines, dedup=True) == [
        "error: connect failed at svc.py:* (x2)"
    ]


def test_compress_errors_caps_at_20_entries() -> None:
    comp = LogCompressor()
    many = [f"error: unique_{i}" for i in range(25)]
    deduped = comp.compress_errors(many, dedup=True)
    raw = comp.compress_errors(many, dedup=False)
    assert deduped == [f"error: unique_{i}" for i in range(5, 25)]
    assert len(raw) == 20


def test_normalize_error_strips_timestamps_and_locations() -> None:
    normalized = LogCompressor()._normalize_error(
        "2026-01-01T10:00:00 ERROR at src/app.py:123   failed\tbad"
    )
    assert normalized == "ERROR at src/app.py:* failed bad"


def test_compress_small_general_output() -> None:
    summary = LogCompressor().compress(["hello world"], "echo hi")
    assert summary.lines[0] == "[1行]"
    assert "通用命令" in summary.lines[1]
    assert summary.total_lines == 1
    assert (summary.warnings, summary.errors) == (0, 0)
    assert summary.latest_message == "hello world"
    assert summary.error_lines == []


def test_compress_large_window_reports_truncation() -> None:
    comp = LogCompressor(max_lines=5)
    lines = [f"compiling module_{i}" for i in range(30)]
    summary = comp.compress(lines, "cargo build --release")
    assert summary.output_type is OutputType.COMPILATION
    assert summary.lines[0] == "[30行，显示最近5行]"
    assert summary.total_lines == 30


def test_compress_includes_progress_line_when_detected() -> None:
    summary = LogCompressor().compress(["downloading 45% ..."], "fetch parts")
    assert summary.progress == "45%"
    assert "进度: 45%" in summary.lines


def test_compress_error_lines_respect_show_errors_config() -> None:
    comp = LogCompressor()
    lines = ["error: alpha", "error: beta", "plain line"]

    shown = comp.compress(
        lines,
        "run",
        LogCompressorConfig(
            compress_threshold=2, recent_lines=1, show_errors=True, dedup_errors=False
        ),
    )
    assert shown.error_lines == ["error: alpha", "error: beta"]
    assert "错误列表:" in shown.lines
    # recent_lines=1：summary 文本里只列 1 条，error_lines 仍带全量
    assert sum(1 for line in shown.lines if line.startswith("  - ")) == 1

    hidden = comp.compress(
        lines,
        "run",
        LogCompressorConfig(
            compress_threshold=2, recent_lines=1, show_errors=False, dedup_errors=False
        ),
    )
    assert hidden.error_lines == ["error: alpha", "error: beta"]
    assert "错误列表:" not in hidden.lines


# ═════════════════════════ ProcessManager ═════════════════════════


def test_init_with_blocked_log_dir_degrades_gracefully(tmp_path: Path) -> None:
    """log_dir 的父路径是文件 → mkdir 失败仅告警，不影响管理器可用。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")

    mgr = ProcessManager(log_dir=blocker / "logs")

    assert mgr.log_dir == blocker / "logs"
    assert mgr.active_processes == {}


def test_sweep_stale_logs_survives_glob_failure(tmp_path: Path, monkeypatch) -> None:
    """启动清扫遇 IO 故障（glob 失败）→ 告警跳过，不影响构造。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    real_glob = Path.glob

    def fake_glob(self: Path, pattern: str) -> Any:
        if self == log_dir:
            raise OSError("sweep io broke")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", fake_glob)
    mgr = ProcessManager(log_dir=log_dir)
    assert mgr.active_processes == {}


def test_sweep_stale_logs_keeps_unremovable_file(tmp_path: Path, monkeypatch) -> None:
    """过期日志删除失败（OSError）→ 保留现场不中断清扫。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old_log = log_dir / "bash_7.log"
    old_log.write_text("old", encoding="utf-8")
    stale = time.time() - 8 * 24 * 3600
    os.utime(old_log, (stale, stale))

    real_unlink = Path.unlink

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == old_log:
            raise OSError("file locked")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    ProcessManager(log_dir=log_dir)

    assert old_log.exists()


def test_unlink_command_log_swallows_os_error(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    target = tmp_path / "bash_9.log"
    target.write_text("x", encoding="utf-8")

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == target:
            raise OSError("permission denied")
        raise AssertionError("不应删除其他文件")

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    mgr._unlink_command_log(target)
    assert target.exists()


def test_write_log_header_failure_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    mgr = _make_manager(tmp_path)
    mgr._write_log_header(blocker / "bash_1.log", "echo hi", 1, owner="sess")  # 不抛


def test_append_to_log_failure_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    mgr = _make_manager(tmp_path)
    mgr._append_to_log(blocker / "bash_1.log", "output chunk")  # 不抛


def test_read_tail_lines_degrades_to_empty_on_io_error(tmp_path: Path) -> None:
    """把目录当日志读 → OSError → 空窗口降级，不抛。"""
    d = tmp_path / "as_dir"
    d.mkdir()
    assert ProcessManager._read_tail_lines(d) == []


def test_ensure_log_dir_returns_path_even_when_mkdir_fails(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    mgr = _make_manager(tmp_path)
    resolved = mgr._ensure_log_dir(blocker / "sub")
    assert resolved == (blocker / "sub").resolve()


def test_normalize_local_command_passthrough_branches(
    tmp_path: Path, monkeypatch
) -> None:
    """非 Windows 宿主 / 显式关闭转换 → 命令原样返回。"""
    mgr = _make_manager(tmp_path)
    monkeypatch.setenv("AO_BASH_WSL_PATH_CONVERT", "0")
    assert mgr._normalize_local_command("cat D:\\a\\b", is_windows=True) == "cat D:\\a\\b"
    monkeypatch.delenv("AO_BASH_WSL_PATH_CONVERT", raising=False)
    assert mgr._normalize_local_command("cat D:\\a\\b", is_windows=False) == "cat D:\\a\\b"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (r'echo "a\;b"', False),  # 双引号内反斜杠转义：分号不算复合
        (r'echo "cost \$5 and $(date)"', True),  # 双引号内 $() 仍算复合
        ("echo $(whoami)", True),  # 顶层命令替换算复合
        ("echo a\nb", True),  # 换行是控制操作符
    ],
)
def test_is_compound_command_extra_cases(command: str, expected: bool) -> None:
    assert ProcessManager._is_compound_command(command) is expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("wsl --verbose echo hi", ["wsl", "--verbose", "-e", "bash", "-c", "echo hi"]),
        ("wsl -u me echo $HOME", ["wsl", "-u", "me", "-e", "bash", "-c", "echo $HOME"]),
    ],
)
def test_parse_wsl_args_flags_with_and_without_value(
    command: str, expected: list[str]
) -> None:
    assert ProcessManager._parse_wsl_args(command) == expected


# ─────────────────────── 本地 spawn：WSL bash LANG 注入 ───────────────────────


class _SpawnRecorder:
    """捕获 create_subprocess_exec 调用参数，返回哨兵进程。"""

    def __init__(self) -> None:
        self.exec_calls: list[tuple[Any, ...]] = []

    async def exec(self, *args: Any, **kwargs: Any) -> Any:
        self.exec_calls.append(args)
        return SimpleNamespace(pid=1)

    async def shell(self, cmd: str, **kwargs: Any) -> Any:
        return SimpleNamespace(pid=2)


@pytest.fixture
def spawn_recorder(monkeypatch: pytest.MonkeyPatch) -> _SpawnRecorder:
    rec = _SpawnRecorder()
    import process_manager as pm

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", rec.exec)
    monkeypatch.setattr(pm.asyncio, "create_subprocess_shell", rec.shell)
    return rec


async def test_spawn_local_wsl_bash_injects_lang(
    tmp_path: Path, spawn_recorder: _SpawnRecorder
) -> None:
    """WSL bash 分支：无 LANG 的环境注入 en_US.UTF-8 后再拉起。"""
    mgr = _make_manager(tmp_path)
    env: dict[str, str] = {"PATH": "x"}

    await mgr._spawn_local_process("echo hi", None, env, is_windows=True)

    assert env["LANG"] == "en_US.UTF-8"
    assert spawn_recorder.exec_calls == [("wsl", "-e", "bash", "-c", "echo hi")]


# ─────────────────────── 容器路径：复合命令包装 + owner ───────────────────────


class _Stream:
    """一次性读完缓冲的假流。"""

    def __init__(self, data: bytes = b"") -> None:
        self._buf = data

    async def read(self, n: int = -1) -> bytes:
        head, self._buf = self._buf, b""
        return head

    async def readline(self) -> bytes:
        idx = self._buf.find(b"\n")
        if idx < 0:
            head, self._buf = self._buf, b""
            return head
        head, self._buf = self._buf[: idx + 1], self._buf[idx + 1 :]
        return head


class _FakeContainerProc:
    """假 docker exec 进程：stdout 首行报容器 pid，随后 EOF。"""

    def __init__(self, host_pid: int, container_pid: int) -> None:
        self.pid = host_pid
        self.returncode: int | None = None
        self.stdout = _Stream(f"{container_pid}\n".encode())
        self.stderr = _Stream()

    async def wait(self) -> int:
        self.returncode = 0
        return 0


async def test_container_compound_command_wraps_in_sh_c_with_owner(
    tmp_path: Path, monkeypatch
) -> None:
    """复合命令套 exec sh -c（pipefail 兜底）；owner 写入 metadata 与日志头。"""
    import process_manager as pm

    mgr = _make_manager(tmp_path)
    fake_proc = _FakeContainerProc(host_pid=42, container_pid=100)
    captured: list[tuple[Any, ...]] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured.append(args)
        return fake_proc

    monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", fake_exec)

    pid, log_file = await mgr.start_process(
        command="echo tag && ls -la",
        container_id="cid-1",
        owner="sess-1",
    )

    assert pid == 42
    args = captured[0]
    assert args[0] == "docker"
    wrapped = args[args.index("-c") + 1]
    assert "echo $$" in wrapped
    assert "exec sh -c" in wrapped
    assert "pipefail" in wrapped
    assert "echo tag && ls -la" in wrapped

    info = mgr.active_processes[42]
    assert info.metadata["container_pid"] == 100
    assert info.metadata["container_id"] == "cid-1"
    assert info.metadata["owner"] == "sess-1"

    await mgr.wait_output_settled(42)
    await asyncio.sleep(0.05)
    assert "# Owner: sess-1" in log_file.read_text(encoding="utf-8")


# ─────────────────────── _read_output：流故障收尾 ───────────────────────


async def test_read_output_stream_failure_still_finalizes(tmp_path: Path) -> None:
    """stdout 读取中途故障 → 跳过该流，进程退出码/状态照常落账。"""
    mgr = _make_manager(tmp_path)

    class BrokenStream:
        async def read(self, n: int) -> bytes:
            raise RuntimeError("pipe exploded")

    class EofStream:
        async def read(self, n: int) -> bytes:
            return b""

    class DeadProc:
        pid = 171
        returncode = None
        stdout = BrokenStream()
        stderr = EofStream()

        async def wait(self) -> int:
            return 3

    proc = DeadProc()
    info = _register(mgr, 171, process=proc)

    await mgr._read_output(171, proc, info.log_file)

    assert info.status == "error"
    assert info.exit_code == 3
    assert "# Process ended with exit code: 3" in info.log_file.read_text(
        encoding="utf-8"
    )


# ─────────────────────── on_output 回调（真实子进程） ───────────────────────


def _py_print_cmd(text: str) -> str:
    exe = Path(sys.executable).as_posix()
    return f'"{exe}" -c "print({text!r})"'


async def test_on_output_callback_receives_lines_real_subprocess(tmp_path: Path) -> None:
    """真实子进程：每行输出回调收到内容，日志与退出码完整。"""
    mgr = _make_manager(tmp_path)
    received: list[str] = []
    pid, log_file = await mgr.start_process(
        _py_print_cmd("callback_sees_me"), on_output=received.append
    )
    try:
        await mgr.wait_output_settled(pid, timeout=10)
        await asyncio.sleep(0.05)
        content = log_file.read_text(encoding="utf-8")
        assert any("callback_sees_me" in line for line in received)
        assert "callback_sees_me" in content
        assert "# Process ended with exit code: 0" in content
        assert pid not in mgr.active_processes  # 正常退出即时清理
    finally:
        await mgr.shutdown_all()


async def test_on_output_callback_exception_isolated_real_subprocess(
    tmp_path: Path,
) -> None:
    """真实子进程：回调抛异常被隔离，日志写入不受影响。"""
    mgr = _make_manager(tmp_path)

    def exploding_callback(_line: str) -> None:
        raise RuntimeError("progress sink down")

    pid, log_file = await mgr.start_process(
        _py_print_cmd("logged_anyway"), on_output=exploding_callback
    )
    try:
        await mgr.wait_output_settled(pid, timeout=10)
        await asyncio.sleep(0.05)
        content = log_file.read_text(encoding="utf-8")
        assert "logged_anyway" in content
        assert "# Process ended with exit code: 0" in content
    finally:
        await mgr.shutdown_all()


# ─────────────────────── send_input 校验与容错 ───────────────────────


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


async def test_send_input_rejects_forbidden_characters(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=93)
    proc.stdin = _fake_stdin()  # type: ignore[attr-defined]
    _register(mgr, 93, process=proc)

    ok, err = await mgr.send_input(93, "yes\x00please")

    assert ok is False
    assert err is not None and "禁止字符" in err


async def test_send_input_reports_generic_write_failure(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=94)
    proc.stdin = _fake_stdin(error=RuntimeError("stream closed"))  # type: ignore[attr-defined]
    _register(mgr, 94, process=proc)

    ok, err = await mgr.send_input(94, "hello")

    assert ok is False
    assert err is not None and "发送输入失败" in err and "stream closed" in err


async def test_send_input_log_write_failure_does_not_block_send(tmp_path: Path) -> None:
    """输入记录落盘失败只降级（debug），发送结果不受影响。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=95)
    proc.stdin = _fake_stdin()  # type: ignore[attr-defined]
    info = _register(mgr, 95, process=proc)
    info.log_file = blocker / "bash_95.log"

    ok, err = await mgr.send_input(95, "echo hello")

    assert (ok, err) == (True, None)


# ─────────────────────── terminate / reap / shutdown 容错 ───────────────────────


async def test_terminate_process_kills_when_graceful_wait_times_out(
    tmp_path: Path, monkeypatch
) -> None:
    """优雅等待超时（注入 wait_for 超时=假时钟快进）→ 非 force 回退 kill()。"""
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=181, wait_forever=True)
    backend = FakeBackend()
    info = _register(mgr, 181, process=proc, backend=backend)

    async def fake_wait_for(coro: Any, timeout: float) -> int:
        coro.close()
        raise TimeoutError()

    monkeypatch.setattr("process_manager.asyncio.wait_for", fake_wait_for)

    ok, err = await mgr.terminate_process(181, force=False)

    assert (ok, err) == (True, None)
    assert proc.kill_called == 1
    assert info.status == "terminated"
    assert backend.killed == [(181, False)]


async def test_reap_after_kill_survives_kill_and_wait_failures(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    class ZombieProc:
        pid = 191
        returncode = None

        async def wait(self) -> int:
            raise RuntimeError("wait exploded")

        def kill(self) -> None:
            raise RuntimeError("kill exploded")

    info = _register(mgr, 191, process=ZombieProc())
    await mgr._reap_after_kill(191, info)
    assert info.status == "terminated"


async def test_shutdown_all_swallows_watchdog_teardown_error(tmp_path: Path) -> None:
    """看门狗任务在取消时抛非取消异常 → 收尾吞掉并置空引用。"""
    mgr = _make_manager(tmp_path)

    async def stubborn() -> None:
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            raise RuntimeError("teardown exploded")

    mgr._watchdog_task = asyncio.get_running_loop().create_task(stubborn())
    await asyncio.sleep(0)

    killed = await mgr.shutdown_all()

    assert killed == 0
    assert mgr._watchdog_task is None


async def test_shutdown_all_counts_terminate_rejections(tmp_path: Path) -> None:
    """terminate 返回失败（无进程对象）→ 不计数、不中断其余清理。"""
    mgr = _make_manager(tmp_path)
    _register(mgr, 221, process=None)  # running 但进程对象缺失 → terminate 拒绝

    killed = await mgr.shutdown_all()

    assert killed == 0


async def test_shutdown_all_swallows_terminate_exception(
    tmp_path: Path, monkeypatch
) -> None:
    mgr = _make_manager(tmp_path)
    _register(mgr, 222, process=FakeProcess(pid=222))

    async def boom(pid: int, force: bool = False) -> tuple[bool, str | None]:
        raise RuntimeError("terminator exploded")

    monkeypatch.setattr(mgr, "terminate_process", boom)

    killed = await mgr.shutdown_all()

    assert killed == 0


async def test_watchdog_kill_without_backend_falls_back_to_terminate(
    tmp_path: Path,
) -> None:
    """无 backend 的旧进程 → 看门狗回退 terminate_process（psutil 无此进程即跳过）。"""
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=2**22, returncode=0)  # 不可能存在的真实 pid
    info = _register(mgr, 2**22, process=proc, backend=None)
    info.log_file.parent.mkdir(parents=True, exist_ok=True)

    await mgr._watchdog_kill(2**22, info, "orphan")

    assert info.status == "terminated"
    # 回退路径走 terminate_process → 日志记 "by user"（非 _reap_after_kill 的 "by watchdog"）
    assert "# Process terminated by user" in info.log_file.read_text(encoding="utf-8")


# ─────────────────────── 看门狗主循环与早退 ───────────────────────


async def test_watchdog_loop_survives_check_errors_and_stops_on_cancel(
    tmp_path: Path, monkeypatch
) -> None:
    """巡检异常不致命（继续下一轮）；CancelledError 优雅退出循环。"""
    mgr = _make_manager(tmp_path)
    calls: list[str] = []

    async def fake_check() -> None:
        calls.append("check")
        if len(calls) == 1:
            raise RuntimeError("check exploded")
        raise asyncio.CancelledError()

    mgr._watchdog_interval = 0.01
    monkeypatch.setattr(mgr, "_watchdog_check_once", fake_check)
    loop_task = asyncio.get_running_loop().create_task(mgr._watchdog_loop())
    await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(calls) == 2
    assert loop_task.cancelled() is False
    assert loop_task.exception() is None


async def test_watchdog_check_once_skips_when_all_processes_already_exited(
    tmp_path: Path,
) -> None:
    """running 快照经同步轮询后全部已退出 → 早退，不做水位采样。"""
    mgr = _make_manager(tmp_path)
    backend = FakeBackend(memory_ratios=[0.99])
    mgr._memory_backend = backend
    _register(mgr, 161, process=FakeProcess(pid=161, returncode=0), backend=backend)

    await mgr._watchdog_check_once()

    assert mgr.active_processes[161].status == "completed"
    assert backend._memory_ratios == [0.99]  # 未触发水位采样
    assert backend.killed == []


async def test_wait_output_settled_swallows_wait_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """asyncio.wait 故障 → 吞掉放行（磁盘日志是兜底真理源）。"""
    mgr = _make_manager(tmp_path)

    async def pending() -> None:
        await asyncio.sleep(30)

    task = asyncio.get_running_loop().create_task(pending())
    _register(mgr, 231, output_task=task)

    async def broken_wait(*args: Any, **kwargs: Any) -> None:
        raise ValueError("wait exploded")

    monkeypatch.setattr("process_manager.asyncio.wait", broken_wait)
    await mgr.wait_output_settled(231, timeout=1.0)  # 不抛

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ─────────────────────── 读面与惰性清理 ───────────────────────


def test_read_log_by_pid_tolerates_unparsable_exit_code(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    log = mgr.log_dir / "bash_211.log"
    log.write_text(
        "# Command: echo hi\n"
        "out line\n"
        "# Process ended with exit code: not-a-number\n",
        encoding="utf-8",
    )

    data = mgr.read_log_by_pid(211)

    assert data is not None
    assert data["exit_code"] is None
    assert "out line" in data["output"]


def test_lazy_cleanup_evicts_finished_processes_over_limit(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    for i in range(101):
        _register(mgr, 1000 + i, status="completed")
    runner = _register(mgr, 2001, process=FakeProcess(pid=2001, returncode=None))

    info = mgr.get_process_info(2001)

    assert info is runner
    assert set(mgr.active_processes) == {2001}


async def test_on_output_task_done_with_raised_task_cleans_entry(tmp_path: Path) -> None:
    """输出任务异常收尾（非取消）→ 记录后照常清理完成态条目，不向上抛。"""

    async def boom() -> None:
        raise RuntimeError("reader crashed")

    mgr = _make_manager(tmp_path)
    task = asyncio.get_running_loop().create_task(boom())
    with contextlib.suppress(RuntimeError):
        await task
    _register(mgr, 241, status="completed")

    mgr._on_output_task_done(241, task)

    assert 241 not in mgr.active_processes


# ─────────────────────── LocalProcessBackend（psutil 异常面） ───────────────────────


def _psutil_stub() -> SimpleNamespace:
    return SimpleNamespace(
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
    )


def test_kill_tree_noop_when_psutil_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)
    LocalProcessBackend._kill_tree_sync(2**22, force=True)  # 不抛


def test_kill_tree_children_enumeration_failure_yields_empty(monkeypatch) -> None:
    """枚举后代时进程已消失 → 视为无后代，仍杀根。"""
    stub = _psutil_stub()
    killed: list[str] = []

    class RootOnly:
        def children(self, recursive: bool = True) -> list[Any]:
            raise stub.NoSuchProcess("gone during enumerate")  # type: ignore[attr-defined]

        def kill(self) -> None:
            killed.append("root")

    stub.Process = lambda pid: RootOnly()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    LocalProcessBackend._kill_tree_sync(2**22, force=True)

    assert killed == ["root"]


def test_kill_tree_skips_descendant_that_vanished_mid_kill(monkeypatch) -> None:
    stub = _psutil_stub()
    killed: list[str] = []

    class Child:
        def kill(self) -> None:
            raise stub.NoSuchProcess("already gone")  # type: ignore[attr-defined]

    class Root:
        def children(self, recursive: bool = True) -> list[Any]:
            return [Child()]

        def kill(self) -> None:
            killed.append("root")

    stub.Process = lambda pid: Root()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    LocalProcessBackend._kill_tree_sync(2**22, force=True)

    assert killed == ["root"]


@pytest.mark.parametrize(("force", "scenario"), [(True, "gone"), (False, "denied")])
def test_kill_tree_root_removal_failures_are_swallowed(
    monkeypatch, force: bool, scenario: str
) -> None:
    """根进程杀失败（NoSuchProcess/AccessDenied）→ 静默收尾，不抛。"""
    stub = _psutil_stub()

    class RootOnly:
        def children(self, recursive: bool = True) -> list[Any]:
            return []

        def kill(self) -> None:
            raise stub.NoSuchProcess("root gone")  # type: ignore[attr-defined]

        def terminate(self) -> None:
            raise stub.AccessDenied("denied")  # type: ignore[attr-defined]

    stub.Process = lambda pid: RootOnly()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    LocalProcessBackend._kill_tree_sync(2**22, force=force)  # 不抛


async def test_local_backend_sample_memory_returns_none_on_error(monkeypatch) -> None:
    import psutil as real_psutil

    def broken() -> Any:
        raise RuntimeError("mem sampling down")

    monkeypatch.setattr(real_psutil, "virtual_memory", broken)
    assert await LocalProcessBackend().sample_memory() is None


async def test_local_backend_sample_unit_memory_sums_tree(monkeypatch) -> None:
    """单进程采样 = 自身 RSS + 后代 RSS；消失的后代跳过。"""
    stub = SimpleNamespace(
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
    )

    def mem(rss: int) -> SimpleNamespace:
        return SimpleNamespace(rss=rss)

    class VanishedChild:
        def memory_info(self) -> SimpleNamespace:
            raise stub.NoSuchProcess("child gone")  # type: ignore[attr-defined]

    class HealthyChild:
        def memory_info(self) -> SimpleNamespace:
            return mem(50)

    class Root:
        def memory_info(self) -> SimpleNamespace:
            return mem(100)

        def children(self, recursive: bool = True) -> list[Any]:
            return [HealthyChild(), VanishedChild()]

    stub.Process = lambda pid: Root()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    total = await LocalProcessBackend().sample_unit_memory(WorkUnit(pid=7, command="x"))
    assert total == 150


async def test_local_backend_sample_unit_memory_missing_process(monkeypatch) -> None:
    stub = _psutil_stub()

    def gone(pid: int) -> Any:
        raise stub.NoSuchProcess(pid)  # type: ignore[attr-defined]

    stub.Process = gone  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)
    assert (
        await LocalProcessBackend().sample_unit_memory(WorkUnit(pid=8, command="x"))
        is None
    )


async def test_local_backend_sample_unit_memory_unexpected_error(monkeypatch) -> None:
    stub = _psutil_stub()

    def broken(pid: int) -> Any:
        raise RuntimeError("psutil exploded")

    stub.Process = broken  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)
    assert (
        await LocalProcessBackend().sample_unit_memory(WorkUnit(pid=9, command="x"))
        is None
    )


# ─────────────────────── ContainerProcessBackend._run_cmd ───────────────────────


async def test_container_backend_run_cmd_returns_process_result() -> None:
    """真实子进程：_run_cmd 返回 (returncode, stdout, stderr) 三元组。"""
    backend = ContainerProcessBackend("unused-cid")
    exe = Path(sys.executable).as_posix()
    code, out, err = await backend._run_cmd(
        [exe, "-c", "print('run_cmd_ok')"], timeout=15
    )
    assert code == 0
    assert b"run_cmd_ok" in out
    assert err == b""
