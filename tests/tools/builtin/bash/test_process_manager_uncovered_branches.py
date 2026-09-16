# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""process_manager 未覆盖分支补测。

按行为契约组织（不断言内部实现）：
- 日志面容错：目录创建/头部写入/追加/清扫/单文件删除的 IO 故障只告警，
  不影响命令执行；
- 容器包装：复合命令套 `exec sh -c`（内层 pipefail），单命令直接 exec；
  owner 落 metadata 与日志头；
- 本地 shell 选择：WSL bash 分支注入 LANG（且不覆盖已设值）；无 WSL/
  显式关闭/非 Windows 时命令原样透传；
- 复合命令扫描：双引号内反斜杠转义跳过、顶层 $( 命令替换判复合；
- 输出读取：on_output 回调异常隔离与逐行回调（关键路径走真实子进程）、
  流读故障不丢退出码、输出任务自身异常被记录且不清理 running 进程；
- 输入面：非法输入（禁止字符/超长）拒绝且不写 stdin、写 stdin 泛化失败
  上报、输入日志 IO 故障容忍；
- 终止/收尾：温和终止超时后升级强杀、wait 吞掉异常、磁盘日志退出码解析
  容错、进程数超限懒惰清理（含 100 边界）；
- 看门狗：主循环取消退出/异常存续、全员退出早退、无 backend 回退
  terminate、reap 强杀失败吞掉、shutdown_all 记录终止失败与看门狗异常；
- 后端：psutil 缺失/枚举失败/杀失败逐级降级、内存采样失败返回 None、
  ContainerProcessBackend._run_cmd 走真实短命子进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import shutil
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bash_types import ProcessInfo
from input_handler import InputHandler
from process_manager import (
    ContainerProcessBackend,
    LocalProcessBackend,
    ProcessManager,
)

pytestmark = pytest.mark.unit


# ─────────────────────────── 桩与助手 ───────────────────────────


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

    async def kill(self, unit: Any, force: bool = True) -> None:
        if self._kill_error is not None:
            raise self._kill_error
        self.killed.append((unit.pid, force))

    async def sample_memory(self) -> float | None:
        if self._memory_ratios:
            return self._memory_ratios.pop(0)
        return None

    async def sample_unit_memory(self, unit: Any) -> int | None:
        return self._unit_rss.get(unit.pid)


class FakeProcess:
    """假 asyncio 进程：可编程 wait 行为与 returncode。"""

    def __init__(
        self,
        pid: int = 4321,
        *,
        returncode: int | None = None,
        wait_forever: bool = False,
        kill_error: Exception | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self._wait_forever = wait_forever
        self._kill_error = kill_error
        self.kill_called = 0

    async def wait(self) -> int:
        if self._wait_forever:
            await asyncio.sleep(1000)
        return self.returncode or 0

    def kill(self) -> None:
        self.kill_called += 1
        if self._kill_error is not None:
            raise self._kill_error
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


@pytest.fixture
def fast_timeout(monkeypatch):
    """把 wait_for 的超时按 1/10 缩短（保留真实计时器与 TimeoutError 机制，
    注入可调延迟而非零延迟 mock），避免 terminate/reap 的 5s/3s 兜底拖慢测试。
    内层超时恒小于外层包装超时（同比例缩短），不会误伤被测协程。"""
    import process_manager as pm_mod

    real_wait_for = asyncio.wait_for

    async def _shrunk(fut: Any, timeout: float | None = None) -> Any:
        if isinstance(timeout, (int, float)) and timeout > 1:
            timeout = timeout / 10.0
        return await real_wait_for(fut, timeout=timeout)

    monkeypatch.setattr(pm_mod.asyncio, "wait_for", _shrunk)


def _require_bash() -> None:
    if shutil.which("bash") is None:
        pytest.skip("关键路径真实子进程需要 bash（Git Bash/POSIX）")


async def _wait_cleaned(mgr: ProcessManager, pid: int, timeout: float = 15.0) -> None:
    """等进程输出任务收尾并从 active_processes 清理（即时清理完成）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid not in mgr.active_processes:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"pid={pid} 输出任务未在 {timeout}s 内收尾")


def _psutil_stub() -> ModuleType:
    """构造假 psutil 模块（函数内 `import psutil` 命中 sys.modules 存根）。"""
    mod = ModuleType("psutil")
    mod.NoSuchProcess = type("NoSuchProcess", (Exception,), {})  # type: ignore[attr-defined]
    mod.AccessDenied = type("AccessDenied", (Exception,), {})  # type: ignore[attr-defined]
    return mod


class _FakeStream:
    """按块预置字节的假流（读尽即 EOF）。"""

    def __init__(self, *chunks: bytes) -> None:
        self._buf = bytearray()
        for c in chunks:
            self._buf.extend(c)

    async def read(self, n: int = -1) -> bytes:
        if not self._buf:
            await asyncio.sleep(0)
            return b""
        take = min(n, len(self._buf)) if n > 0 else len(self._buf)
        chunk = bytes(self._buf[:take])
        del self._buf[:take]
        return chunk

    async def readline(self) -> bytes:
        """按行读（_read_container_pid 用），无行时让出控制权后报 EOF。"""
        if not self._buf:
            await asyncio.sleep(0)
            return b""
        idx = self._buf.find(b"\n")
        if idx < 0:
            line = bytes(self._buf)
            self._buf.clear()
            return line
        line = bytes(self._buf[: idx + 1])
        del self._buf[: idx + 1]
        return line


def _fake_container_process(host_pid: int, container_pid: int) -> MagicMock:
    """docker exec 假进程：stdout 第一行报容器内 pid，随后 EOF。"""
    proc = MagicMock()
    proc.pid = host_pid
    proc.returncode = None
    proc.stdout = _FakeStream(f"{container_pid}\n".encode())
    proc.stderr = _FakeStream()
    proc.stdin = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


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


# ─────────────────────────── 日志面容错 ───────────────────────────


def test_init_tolerates_uncreatable_log_dir(tmp_path, caplog):
    """日志目录不可创建（父级是文件）→ 告警且不阻断管理器构建。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="process_manager"):
        mgr = ProcessManager(log_dir=blocked / "logs")
    assert mgr.active_processes == {}
    assert any("日志目录创建失败" in r.getMessage() for r in caplog.records)


def test_ensure_log_dir_tolerates_failure_and_returns_resolved(tmp_path, caplog):
    """_ensure_log_dir：目录不可建时告警并仍返回 resolved 路径。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    mgr = _make_manager(tmp_path)
    with caplog.at_level(logging.WARNING, logger="process_manager"):
        resolved = mgr._ensure_log_dir(blocked / "sub")
    assert Path(resolved).name == "sub"
    assert any("日志目录创建失败" in r.getMessage() for r in caplog.records)


def test_startup_sweep_tolerates_unreadable_dir(caplog):
    """清扫：目录 glob 即 IO 故障 → 告警并放弃本轮，不阻断启动。"""

    class _UnreadableDir:
        def mkdir(self, parents: bool = True, exist_ok: bool = True) -> None:
            return None

        def glob(self, pattern: str):
            raise OSError("directory unreadable")

    with caplog.at_level(logging.WARNING, logger="process_manager"):
        ProcessManager(log_dir=_UnreadableDir())
    assert any("启动日志清扫失败" in r.getMessage() for r in caplog.records)


def test_startup_sweep_tolerates_unstatable_file(caplog):
    """清扫：单文件 stat 故障 → 告警保留现场，其余扫描继续。"""

    class _BrokenStatFile:
        def is_file(self) -> bool:
            return True

        def stat(self):
            raise OSError("stat failed")

    class _DirWithBrokenFile:
        def mkdir(self, parents: bool = True, exist_ok: bool = True) -> None:
            return None

        def glob(self, pattern: str):
            return [_BrokenStatFile()]

    with caplog.at_level(logging.WARNING, logger="process_manager"):
        ProcessManager(log_dir=_DirWithBrokenFile())
    assert any("过期日志删除失败" in r.getMessage() for r in caplog.records)


def test_flush_deletes_tolerates_unlink_failure(tmp_path, caplog):
    """宽限期删除：单文件 unlink 失败 → 告警保留现场，条目照常出队。"""
    mgr = _make_manager(tmp_path, log_delete_grace_seconds=0.0)

    class _LockedPath:
        def unlink(self, missing_ok: bool = False) -> None:
            raise PermissionError("file locked")

    mgr._logs_pending_delete.append((_LockedPath(), time.monotonic() - 1.0))  # noqa: SLF001
    mgr.flush_expired_log_deletes()
    assert any("命令日志清理失败" in r.getMessage() for r in caplog.records)
    assert mgr._logs_pending_delete == []


def test_write_log_header_io_failure_is_tolerated(tmp_path, caplog):
    """日志头写入失败（目标是目录）→ 告警不抛。"""
    mgr = _make_manager(tmp_path)
    as_dir = tmp_path / "as_dir"
    as_dir.mkdir()
    mgr._write_log_header(as_dir, "echo hi", 5)
    assert any("日志头部写入失败" in r.getMessage() for r in caplog.records)


def test_write_log_header_success_contains_owner_slot(tmp_path):
    """正常写入：pid/owner/命令齐备，owner 行位于头部供降级路径解析。"""
    mgr = _make_manager(tmp_path)
    log_file = tmp_path / "bash_6.log"
    mgr._write_log_header(log_file, "echo hi", 6, owner="sess-a")
    text = log_file.read_text(encoding="utf-8")
    assert "# PID: 6" in text
    assert "# Owner: sess-a" in text
    assert "echo hi" in text


def test_append_to_log_io_failure_is_tolerated(tmp_path, caplog):
    """日志追加失败（目标是目录）→ 告警不抛；成功路径按序追加。"""
    mgr = _make_manager(tmp_path)
    as_dir = tmp_path / "as_dir"
    as_dir.mkdir()
    mgr._append_to_log(as_dir, "content")
    assert any("日志追加写入失败" in r.getMessage() for r in caplog.records)

    real_log = tmp_path / "bash_7.log"
    mgr._append_to_log(real_log, "chunk-a")
    mgr._append_to_log(real_log, "chunk-b")
    assert real_log.read_text(encoding="utf-8") == "chunk-achunk-b"


def test_read_tail_lines_io_error_returns_empty(tmp_path):
    """尾部读取遇 IO 故障 → 空窗口降级，不抛。"""
    as_dir = tmp_path / "as_dir"
    as_dir.mkdir()
    assert ProcessManager._read_tail_lines(as_dir) == []


# ─────────────────────────── 容器包装与 owner ───────────────────────────


@pytest.mark.asyncio
async def test_container_compound_command_wraps_with_inner_sh(tmp_path):
    """复合命令：容器路径必须套 `exec sh -c`（内层 pipefail），否则 exec 只保留
    第一段、后续段输出丢失。"""
    mgr = _make_manager(tmp_path)
    captured: list = []

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return _fake_container_process(host_pid=42, container_pid=100)

    with patch("process_manager.asyncio.create_subprocess_exec", side_effect=fake_exec):
        pid, _ = await mgr.start_process(command='echo "标签" && ls', container_id="cid")

    assert pid == 42
    wrapped = captured[captured.index("-c") + 1]
    assert wrapped.startswith("echo $$; exec sh -c "), f"实际包装: {wrapped}"
    assert "set -o pipefail 2>/dev/null; " in wrapped
    assert "&&" in wrapped


@pytest.mark.asyncio
async def test_container_simple_command_wraps_with_direct_exec(tmp_path):
    """单条命令（对照）：直接 `exec <cmd>`，pid 精准、不套内层 sh。"""
    mgr = _make_manager(tmp_path)
    captured: list = []

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return _fake_container_process(host_pid=42, container_pid=100)

    with patch("process_manager.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await mgr.start_process(command="echo hello", container_id="cid")

    wrapped = captured[captured.index("-c") + 1]
    assert wrapped == "echo $$; exec echo hello"
    assert "sh -c" not in wrapped


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "sess-a"])
async def test_container_process_records_owner_in_metadata_and_header(tmp_path, owner):
    """owner 会话身份：落 metadata（越权校验用）与日志头 `# Owner:`；缺省时两者
    均不出现。"""
    mgr = _make_manager(tmp_path)
    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=_fake_container_process(42, 100),
    ):
        pid, log_file = await mgr.start_process(
            command="sleep 1", container_id="cid", owner=owner
        )

    info = mgr.get_process_info(pid)
    assert info is not None
    header = log_file.read_text(encoding="utf-8")
    if owner is None:
        assert "owner" not in info.metadata
        assert "# Owner:" not in header
    else:
        assert info.metadata["owner"] == owner
        assert f"# Owner: {owner}" in header


# ─────────────────────────── 本地命令归一化与 shell 选择 ───────────────────────────


@pytest.mark.parametrize(
    ("which_wsl", "env_value", "is_windows", "expected"),
    [
        ("C:\\Windows\\system32\\wsl.EXE", None, True, "cat /mnt/d/a/b"),
        (None, None, True, "cat D:\\a\\b"),
        ("C:\\Windows\\system32\\wsl.EXE", "0", True, "cat D:\\a\\b"),
        ("C:\\Windows\\system32\\wsl.EXE", None, False, "cat D:\\a\\b"),
    ],
)
def test_normalize_local_command_guard_matrix(
    tmp_path, monkeypatch, which_wsl, env_value, is_windows, expected
):
    """WSL 路径转换守卫矩阵：有 wsl 且启用才转换；无 wsl/显式关闭/非 Windows
    一律原样透传。"""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(shutil, "which", lambda name: which_wsl if name == "wsl" else None)
    if env_value is None:
        monkeypatch.delenv("AO_BASH_WSL_PATH_CONVERT", raising=False)
    else:
        monkeypatch.setenv("AO_BASH_WSL_PATH_CONVERT", env_value)
    mgr = _make_manager(tmp_path)
    assert mgr._normalize_local_command(r"cat D:\a\b", is_windows=is_windows) == expected


@pytest.fixture
def spawn_recorder(monkeypatch) -> _SpawnRecorder:
    rec = _SpawnRecorder()
    import process_manager as pm_mod

    monkeypatch.setattr(pm_mod.asyncio, "create_subprocess_exec", rec.exec)
    monkeypatch.setattr(pm_mod.asyncio, "create_subprocess_shell", rec.shell)
    return rec


@pytest.mark.asyncio
@pytest.mark.parametrize("preset_lang", [False, True])
async def test_spawn_local_wsl_bash_sets_lang_once(tmp_path, monkeypatch, spawn_recorder, preset_lang):
    """WSL bash 分支：仅在调用方未设 LANG 时注入 en_US.UTF-8，不覆盖已有值。"""
    monkeypatch.setattr(shutil, "which", lambda name: "wsl.exe" if name == "wsl" else None)
    mgr = _make_manager(tmp_path)
    env = {"PATH": "x"}
    if preset_lang:
        env["LANG"] = "zh_CN.UTF-8"

    await mgr._spawn_local_process("echo hi", None, env, is_windows=True)

    assert spawn_recorder.exec_calls, "应走 wsl -e bash -c exec 分支"
    assert spawn_recorder.exec_calls[0][:3] == ("wsl", "-e", "bash")
    if preset_lang:
        assert env["LANG"] == "zh_CN.UTF-8"
    else:
        assert env["LANG"] == "en_US.UTF-8"


# ─────────────────────────── 复合命令扫描 ───────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('echo "a\\;b"', False),  # 双引号内反斜杠转义分号：不终止扫描也不误判
        ('echo "x\\\\"; echo b', True),  # \" 被转义消耗、引号随后闭合，顶层 ; → 复合
        ("echo $(whoami)", True),  # 顶层 $() 命令替换 → 复合
        ("cd $(pwd) && ls", True),
    ],
)
def test_is_compound_command_escape_and_substitution(command: str, expected: bool) -> None:
    assert ProcessManager._is_compound_command(command) is expected


# ─────────────────────────── 输出读取：回调与流故障（真实子进程） ───────────────────────────


@pytest.mark.asyncio
async def test_real_process_on_output_streams_lines_with_stderr_prefix(tmp_path, monkeypatch):
    """关键路径（真实子进程）：on_output 逐行回调且 stderr 带 [stderr] 前缀；
    进程退出即时清理后磁盘日志仍可按 pid 回读、退出码正确。"""
    _require_bash()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    mgr = ProcessManager(log_dir=tmp_path / "logs")
    collected: list[str] = []
    try:
        pid, log_file = await mgr.start_process(
            "echo out-a; echo err-b 1>&2",
            working_dir=str(tmp_path),
            on_output=collected.append,
        )
        await _wait_cleaned(mgr, pid)
    finally:
        await mgr.shutdown_all()

    joined = "".join(collected)
    assert "out-a\n" in joined
    assert "[stderr] err-b\n" in joined
    assert collected and all(line.endswith("\n") for line in collected)
    assert log_file.exists() and "out-a" in log_file.read_text(encoding="utf-8")
    data = mgr.read_log_by_pid(pid)
    assert data is not None
    assert data["exit_code"] == 0
    assert "out-a" in data["output"] and "err-b" in data["output"]


@pytest.mark.asyncio
async def test_real_process_on_output_exception_isolated(tmp_path, monkeypatch, caplog):
    """on_output 回调抛异常 → 逐次隔离（debug 记录），不影响命令执行与日志完整。"""
    _require_bash()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    mgr = ProcessManager(log_dir=tmp_path / "logs")

    def broken(_line: str) -> None:
        raise RuntimeError("progress sink exploded")

    with caplog.at_level(logging.DEBUG, logger="process_manager"):
        try:
            pid, log_file = await mgr.start_process(
                "echo boom-line; echo second", working_dir=str(tmp_path), on_output=broken
            )
            await _wait_cleaned(mgr, pid)
        finally:
            await mgr.shutdown_all()

    assert any("进度回调异常" in r.getMessage() for r in caplog.records)
    data = mgr.read_log_by_pid(pid)
    assert data is not None
    assert data["exit_code"] == 0, "回调异常不得影响命令执行与退出码"
    assert "boom-line" in data["output"] and "second" in data["output"]


@pytest.mark.asyncio
async def test_read_output_stream_error_keeps_exit_code(tmp_path):
    """流读取故障：读取循环断裂，但退出码/状态/日志收尾照常落盘。"""

    class _BrokenStream:
        async def read(self, n: int = -1) -> bytes:
            raise OSError("pipe exploded")

    class _Proc:
        def __init__(self) -> None:
            self.pid = 777
            self.returncode = None
            self.stdout = _BrokenStream()
            self.stderr = _BrokenStream()

        async def wait(self) -> int:
            return 3

    mgr = _make_manager(tmp_path)
    log_file = mgr.log_dir / "bash_777.log"
    info = ProcessInfo(
        pid=777, command="x", start_time=0.0, log_file=log_file,
        process=_Proc(), status="running",
    )
    mgr.active_processes[777] = info

    await mgr._read_output(777, info.process, log_file)

    assert info.status == "error"
    assert info.exit_code == 3
    assert "# Process ended with exit code: 3" in log_file.read_text(encoding="utf-8")


def test_on_output_task_done_records_task_exception(tmp_path, caplog):
    """输出读取任务自身抛异常 → 记录且不误清 running 进程（保守留看门狗兜底）。"""
    mgr = _make_manager(tmp_path)

    class _FailedTask:
        def result(self):
            raise RuntimeError("reader exploded")

    info = _register(mgr, 211, status="running")
    mgr._on_output_task_done(211, _FailedTask())
    assert any("输出读取任务异常" in r.getMessage() for r in caplog.records)
    assert 211 in mgr.active_processes
    assert info.status == "running"


# ─────────────────────────── 输入面 ───────────────────────────


class _RecordingStdin:
    """记录写入的假 stdin，可注入 write 异常。"""

    def __init__(self, writes: list[bytes], error: Exception | None = None) -> None:
        self.writes = writes
        self._error = error

    def write(self, data: bytes) -> None:
        if self._error is not None:
            raise self._error
        self.writes.append(data)

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expect_fragment"),
    [
        ("has\x00null", "禁止字符"),
        ("x" * 4097, "长度超过限制"),
    ],
)
async def test_send_input_rejects_invalid_payload(tmp_path, payload, expect_fragment):
    """非法输入（含禁止字符 / 超长）→ 拒绝且不写 stdin。"""
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=96)
    writes: list[bytes] = []
    proc.stdin = _RecordingStdin(writes)
    _register(mgr, 96, process=proc)

    ok, err = await mgr.send_input(96, payload)

    assert ok is False
    assert expect_fragment in (err or "")
    assert writes == [], "被拒输入不得写 stdin"


@pytest.mark.asyncio
async def test_send_input_reports_generic_write_failure(tmp_path):
    """写 stdin 抛非管道类异常 → 泛化失败上报（区别于管道断开文案）。"""
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=97)
    proc.stdin = _RecordingStdin([], error=ValueError("transport closed"))
    info = _register(mgr, 97, process=proc)

    ok, err = await mgr.send_input(97, "hi")

    assert ok is False
    assert "发送输入失败" in (err or "")
    assert "管道已断开" not in (err or "")


def test_log_input_masks_sensitive_and_tolerates_io_failure(tmp_path, caplog):
    """输入日志：敏感输入打码落盘；IO 故障（目标是目录）只 debug 不抛。"""
    handler = InputHandler()
    real_log = tmp_path / "bash_301.log"
    ProcessManager._log_input(real_log, "export API_KEY=abc123", handler)
    text = real_log.read_text(encoding="utf-8")
    assert "[INPUT]" in text
    assert "abc123" not in text, "敏感取值必须打码"

    as_dir = tmp_path / "as_dir"
    as_dir.mkdir()
    with caplog.at_level(logging.DEBUG, logger="process_manager"):
        ProcessManager._log_input(as_dir, "hello", handler)  # 不抛
    assert any("输入记录写入失败" in r.getMessage() for r in caplog.records)


# ─────────────────────────── 终止 / 收尾 / 读面 ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("force,expected_kill_calls", [(False, 1), (True, 0)])
async def test_terminate_process_gentle_wait_timeout_escalates(
    tmp_path, fast_timeout, force, expected_kill_calls
):
    """温和终止（force=False）等待超时后升级 process.kill()；强制终止不重复杀。"""
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    proc = FakeProcess(pid=61, wait_forever=True)
    info = _register(mgr, 61, process=proc, backend=backend)

    ok, err = await mgr.terminate_process(61, force=force)

    assert (ok, err) == (True, None)
    assert info.status == "terminated"
    assert proc.kill_called == expected_kill_calls
    assert backend.killed == [(61, force)]


@pytest.mark.asyncio
async def test_wait_output_settled_swallows_wait_errors(tmp_path):
    """输出任务无法被 asyncio.wait 等待（非任务对象）→ 吞掉放行，磁盘日志兜底。"""
    mgr = _make_manager(tmp_path)

    class _NeverDone:
        def done(self) -> bool:
            return False

    _register(mgr, 161, output_task=_NeverDone())
    await mgr.wait_output_settled(161, timeout=0.05)  # 不抛


@pytest.mark.parametrize(
    "tail_line",
    [
        "# Process ended with exit code: N/A",
        "# Process ended with exit code:",
    ],
)
def test_read_log_by_pid_tolerates_malformed_exit_code(tmp_path, tail_line):
    """磁盘日志尾部退出码不可解析 → exit_code=None，其余内容照常返回。"""
    mgr = _make_manager(tmp_path)
    log = mgr.log_dir / "bash_311.log"
    log.write_text(f"out line\n{tail_line}\n", encoding="utf-8")

    data = mgr.read_log_by_pid(311)

    assert data is not None
    assert data["exit_code"] is None
    assert "out line" in data["output"]


def _register_completed(mgr: ProcessManager, pid: int) -> None:
    mgr.active_processes[pid] = ProcessInfo(
        pid=pid,
        command=f"cmd-{pid}",
        start_time=0.0,
        log_file=mgr.log_dir / f"bash_{pid}.log",
        status="completed",
    )


@pytest.mark.parametrize("count,expect_remaining", [(100, 100), (101, 0)])
def test_cleanup_if_needed_boundary(tmp_path, caplog, count, expect_remaining):
    """懒惰清理边界：100 个不触发，101 个触发全清 completed 并留下限日志。"""
    mgr = _make_manager(tmp_path)
    for i in range(count):
        _register_completed(mgr, 7000 + i)

    with caplog.at_level(logging.INFO, logger="process_manager"):
        mgr.get_process_info(7000)

    assert len(mgr.active_processes) == expect_remaining
    over_logs = [r for r in caplog.records if "进程数超过限制" in r.getMessage()]
    assert bool(over_logs) is (count > 100)


# ─────────────────────────── 看门狗主循环与回退 ───────────────────────────


@pytest.mark.asyncio
async def test_watchdog_loop_exits_cleanly_when_cancelled(tmp_path, caplog):
    """主循环被取消（取消点落在巡检内）→ 记录后正常返回，而非任务炸出。"""
    mgr = _make_manager(tmp_path)
    mgr._watchdog_interval = 0.01
    entered = asyncio.Event()
    release = asyncio.Event()

    async def parked_check() -> None:
        entered.set()
        await release.wait()

    mgr._watchdog_check_once = parked_check  # type: ignore[method-assign]
    task = asyncio.get_running_loop().create_task(mgr._watchdog_loop())
    with caplog.at_level(logging.INFO, logger="process_manager"):
        await entered.wait()
        task.cancel()
        await task  # 若 CancelledError 未被主循环吞掉，这里会抛
    assert task.cancelled() is False
    assert any("看门狗任务被取消" in r.getMessage() for r in caplog.records)
    release.set()  # 防御性释放（已无等待者）


@pytest.mark.asyncio
async def test_watchdog_loop_survives_check_exception(tmp_path, caplog):
    """巡检抛泛化异常 → 记录后循环继续下一轮（不能一次异常失去保护）。"""
    mgr = _make_manager(tmp_path)
    mgr._watchdog_interval = 0.01
    calls = {"n": 0}
    parked = asyncio.Event()

    async def flaky_check() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("巡检通道炸了")
        parked.set()
        await asyncio.sleep(3600)

    mgr._watchdog_check_once = flaky_check  # type: ignore[method-assign]
    task = asyncio.get_running_loop().create_task(mgr._watchdog_loop())
    await asyncio.wait_for(parked.wait(), timeout=2.0)

    assert calls["n"] >= 2, "巡检异常后必须继续下一轮"
    assert any("看门狗巡检异常" in r.getMessage() for r in caplog.records)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_watchdog_check_once_returns_when_all_polls_finished(tmp_path):
    """running 快照经轮询同步后全部已退出 → 早退，不做采样/杀。"""
    mgr = _make_manager(tmp_path)
    backend = FakeBackend()
    mgr._memory_backend = backend
    proc = FakeProcess(pid=171, returncode=0)
    info = _register(mgr, 171, process=proc, backend=backend)

    await mgr._watchdog_check_once()

    assert info.status == "completed", "已退出进程应由轮询同步标记"
    assert backend.killed == []


@pytest.mark.asyncio
async def test_watchdog_check_once_high_watermark_triggers_idle_cleanup(tmp_path):
    """内存采样达高水位（≥0.85）→ 巡检触发按 idle 排序清理，先杀最闲。"""
    mgr = _make_manager(tmp_path)
    backend = FakeBackend(memory_ratios=[0.9])
    mgr._memory_backend = backend
    now = time.time()
    _register(mgr, 181, process=FakeProcess(pid=181, returncode=None), backend=backend,
              last_access=now - 100)
    _register(mgr, 182, process=FakeProcess(pid=182, returncode=None), backend=backend,
              last_access=now - 5)

    await mgr._watchdog_check_once()

    assert [pid for pid, _ in backend.killed] == [181], "先杀 idle 最大的最闲进程"
    assert mgr.active_processes[181].status == "terminated"
    assert mgr.active_processes[182].status == "running", "杀到回落前只杀一个（重采样判定）"


@pytest.mark.asyncio
async def test_unit_memory_cleanup_skips_processes_without_backend(tmp_path):
    """单进程内存判据：无 backend 的单元无法采样 → 跳过不误杀；有 backend 的照判。"""
    mgr = _make_manager(tmp_path)
    mgr._unit_memory_limit = 100
    now = time.time()
    backend = FakeBackend(unit_rss={192: 10**9})
    no_backend = _register(mgr, 191, backend=None, last_access=now - 100)
    has_backend = _register(mgr, 192, backend=backend, last_access=now - 1)

    await mgr._cleanup_by_unit_memory([(191, no_backend), (192, has_backend)], now)

    assert [pid for pid, _ in backend.killed] == [192]
    assert no_backend.status == "running", "无 backend 的单元不得被杀"


class _LineStream:
    """预置 n 行文本的假流（read 按块吐出）。"""

    def __init__(self, n_lines: int) -> None:
        self._data = "".join(f"line{i}\n" for i in range(n_lines)).encode()

    async def read(self, n: int = -1) -> bytes:
        if not self._data:
            return b""
        chunk, self._data = self._data[:4096], self._data[4096:]
        return chunk


@pytest.mark.asyncio
async def test_read_output_flushes_batch_at_line_threshold(tmp_path):
    """攒批落盘：超过 512 行阈值即批量落盘，600 行输出全部到日志，无丢失/重复。"""
    mgr = _make_manager(tmp_path)
    log_file = mgr.log_dir / "bash_888.log"

    class _Proc:
        pid = 888
        returncode = None
        stdout = _LineStream(600)
        stderr = _LineStream(0)

        async def wait(self) -> int:
            return 0

    info = ProcessInfo(
        pid=888, command="x", start_time=0.0, log_file=log_file,
        process=_Proc(), status="running",
    )
    mgr.active_processes[888] = info

    await mgr._read_output(888, info.process, log_file)

    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.startswith("line")]
    assert len(lines) == 600
    assert set(lines) == {f"line{i}" for i in range(600)}
    assert info.status == "completed" and info.exit_code == 0


@pytest.mark.asyncio
async def test_watchdog_kill_without_backend_falls_back_to_terminate(tmp_path, monkeypatch):
    """无 backend 的旧进程：看门狗回退 terminate_process（psutil 整树杀）。"""
    import psutil as real_psutil

    def missing(pid: int) -> Any:
        raise real_psutil.NoSuchProcess(pid)

    monkeypatch.setattr(real_psutil, "Process", missing)
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=2**28, returncode=0)
    info = _register(mgr, 2**28, process=proc, backend=None)
    info.log_file.write_text("# Bash Command Log\n", encoding="utf-8")

    await mgr._watchdog_kill(2**28, info, "orphan")

    assert info.status == "terminated"
    # 回退路径经 terminate_process 收尾，落"terminated by user"标记
    assert "# Process terminated by user" in info.log_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_reap_after_kill_swallows_force_kill_failure(tmp_path, fast_timeout):
    """reap：等待超时 → 强杀兜底也失败（如 AccessDenied）→ 吞掉仍标记 terminated。"""
    mgr = _make_manager(tmp_path)
    proc = FakeProcess(pid=62, wait_forever=True, kill_error=PermissionError("denied"))
    info = _register(mgr, 62, process=proc)
    info.log_file.write_text("# Bash Command Log\n", encoding="utf-8")

    await asyncio.wait_for(mgr._reap_after_kill(62, info), timeout=5.0)

    assert proc.kill_called == 1
    assert info.status == "terminated"
    assert "terminated by watchdog" in info.log_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_shutdown_all_records_termination_failure(tmp_path, caplog):
    """shutdown_all：单进程终止失败（进程对象缺失）→ 记录且不计入成功数。"""
    mgr = _make_manager(tmp_path)
    _register(mgr, 191, status="running", process=None)

    killed = await mgr.shutdown_all()

    assert killed == 0
    assert any("终止失败" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_shutdown_all_logs_watchdog_task_error(tmp_path, caplog):
    """shutdown_all：等待看门狗任务退出时任务改抛泛化异常 → 记录并继续清理。"""
    mgr = _make_manager(tmp_path)

    async def stubborn() -> None:
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            raise RuntimeError("stop flow aborted") from None

    mgr._watchdog_task = asyncio.get_running_loop().create_task(stubborn())
    await asyncio.sleep(0)  # 让任务启动进入 sleep

    killed = await mgr.shutdown_all()

    assert killed == 0
    assert mgr._watchdog_task is None
    assert any("watchdog task raised" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("wsl -v ls", ["wsl", "-v", "-e", "bash", "-c", "ls"]),
        ("wsl -u root echo $HOME", ["wsl", "-u", "root", "-e", "bash", "-c", "echo $HOME"]),
    ],
)
def test_parse_wsl_args_other_flags_kept_in_order(command: str, expected: list[str]) -> None:
    """带值标志之外的 WSL 标志原样保留在 wsl_opts（位置不乱序）。"""
    assert ProcessManager._parse_wsl_args(command) == expected


# ─────────────────────────── 后端降级契约 ───────────────────────────


def test_local_backend_kill_tree_without_psutil_is_noop(monkeypatch):
    """psutil 不可用 → 单进程杀整体跳过，交调用方回退，不抛。"""
    monkeypatch.setitem(sys.modules, "psutil", None)
    LocalProcessBackend._kill_tree_sync(2**30, force=True)  # 不抛


def test_local_backend_children_enumeration_failure_kills_root_only(monkeypatch):
    """枚举后代失败（NoSuchProcess）→ 降级为只杀根进程。"""
    stub = _psutil_stub()
    killed: list[str] = []

    class Root:
        def children(self, recursive: bool = True) -> list[Any]:
            raise stub.NoSuchProcess(5)  # type: ignore[attr-defined]

        def kill(self) -> None:
            killed.append("root")

    stub.Process = lambda pid: Root()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    LocalProcessBackend._kill_tree_sync(5, force=True)

    assert killed == ["root"]


def test_local_backend_descendant_kill_failure_continues(monkeypatch):
    """后代单个杀失败（NoSuchProcess）→ 跳过继续，其余后代与根照杀。"""
    stub = _psutil_stub()
    killed: list[str] = []

    class Leaf:
        def __init__(self, name: str, exc: Exception | None = None) -> None:
            self._name = name
            self._exc = exc

        def kill(self) -> None:
            if self._exc is not None:
                raise self._exc
            killed.append(self._name)

    class Root:
        def children(self, recursive: bool = True) -> list[Any]:
            return [
                Leaf("ghost", exc=stub.NoSuchProcess(1)),  # type: ignore[attr-defined]
                Leaf("leaf"),
            ]

        def kill(self) -> None:
            killed.append("root")

    stub.Process = lambda pid: Root()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    LocalProcessBackend._kill_tree_sync(5, force=True)

    assert killed == ["leaf", "root"]


@pytest.mark.parametrize("exc_name", ["NoSuchProcess", "AccessDenied"])
def test_local_backend_root_kill_errors_swallowed(monkeypatch, exc_name: str):
    """根进程杀失败（NoSuchProcess/AccessDenied）→ 吞掉不抛（进程可能已消失/受限）。"""
    stub = _psutil_stub()
    killed: list[str] = []

    class Root:
        def children(self, recursive: bool = True) -> list[Any]:
            return []

        def kill(self) -> None:
            raise getattr(stub, exc_name)(5)

    stub.Process = lambda pid: Root()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    LocalProcessBackend._kill_tree_sync(5, force=True)  # 不抛
    assert killed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stub_setup", "expected"),
    [
        ("raise", None),
        ("percent", 0.555),
    ],
)
async def test_local_backend_sample_memory(monkeypatch, stub_setup: str, expected):
    """宿主内存采样：采样故障返回 None（降级）；正常返回 0~1 比率。"""
    if stub_setup == "raise":
        def boom():
            raise RuntimeError("mem unavailable")

        stub = _psutil_stub()
        stub.virtual_memory = boom  # type: ignore[attr-defined]
    else:
        stub = _psutil_stub()
        stub.virtual_memory = lambda: SimpleNamespace(percent=55.5)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    ratio = await LocalProcessBackend().sample_memory()

    assert ratio == expected
    if ratio is not None:
        assert 0.0 <= ratio <= 1.0


class _StubPsutilProc:
    """假 psutil 进程节点：RSS + 后代，可注入读取异常。"""

    def __init__(
        self,
        rss: int = 0,
        children: list["_StubPsutilProc"] | None = None,
        info_error: Exception | None = None,
        children_error: Exception | None = None,
    ) -> None:
        self._rss = rss
        self._children = children or []
        self._info_error = info_error
        self._children_error = children_error

    def memory_info(self) -> SimpleNamespace:
        if self._info_error is not None:
            raise self._info_error
        return SimpleNamespace(rss=self._rss)

    def children(self, recursive: bool = True) -> list["_StubPsutilProc"]:
        if self._children_error is not None:
            raise self._children_error
        return list(self._children)


@pytest.mark.asyncio
async def test_local_backend_sample_unit_memory_sums_tree(monkeypatch):
    """单进程内存采样：根 RSS + 全部后代 RSS 求和；读失败的后代跳过。"""
    stub = _psutil_stub()
    stub.Process = lambda pid: _StubPsutilProc(  # type: ignore[attr-defined]
        rss=100,
        children=[
            _StubPsutilProc(rss=50),
            _StubPsutilProc(rss=0, info_error=stub.AccessDenied(2)),  # type: ignore[attr-defined]
        ],
    )
    monkeypatch.setitem(sys.modules, "psutil", stub)

    from bash_types import WorkUnit

    total = await LocalProcessBackend().sample_unit_memory(WorkUnit(pid=9, command="x"))

    assert total == 150


@pytest.mark.asyncio
async def test_local_backend_sample_unit_memory_root_missing_returns_none(monkeypatch):
    """根进程已消失/不可访问 → None（采样不可用）。"""
    stub = _psutil_stub()
    stub.Process = lambda pid: (_ for _ in ()).throw(stub.NoSuchProcess(pid))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", stub)

    from bash_types import WorkUnit

    assert await LocalProcessBackend().sample_unit_memory(WorkUnit(pid=10, command="x")) is None


@pytest.mark.asyncio
async def test_local_backend_sample_unit_memory_generic_error_returns_none(monkeypatch):
    """枚举后代抛其他异常 → None（不向看门狗传播）。"""
    stub = _psutil_stub()
    stub.Process = lambda pid: _StubPsutilProc(  # type: ignore[attr-defined]
        rss=10, children_error=RuntimeError("psutil exploded")
    )
    monkeypatch.setitem(sys.modules, "psutil", stub)

    from bash_types import WorkUnit

    assert await LocalProcessBackend().sample_unit_memory(WorkUnit(pid=11, command="x")) is None


@pytest.mark.asyncio
async def test_container_backend_run_cmd_runs_real_child_process():
    """容器后端 _run_cmd 默认实现走真实短命子进程，返回 (rc, stdout, stderr)。"""
    backend = ContainerProcessBackend("cid")
    rc, out, err = await backend._run_cmd(
        [sys.executable, "-c", "print('run_cmd_ok')"], timeout=60
    )
    assert rc == 0
    assert b"run_cmd_ok" in out

def test_map_wsl_working_dir_passthrough_non_workspace_paths():
    """非 /workspace 约定路径原样透传（含反斜杠形态），不嫁接 workspace 前缀。"""
    from process_manager import _map_wsl_working_dir

    backend = {"workspace_wsl": "/mnt/d/ws"}
    assert _map_wsl_working_dir(backend, "/opt/data") == "/opt/data"
    assert _map_wsl_working_dir(backend, "D:\proj\sub") == "D:\proj\sub"
    assert _map_wsl_working_dir({}, "/opt/data") == "/opt/data"


def test_map_wsl_working_dir_maps_workspace_prefix_to_wsl_root():
    """/workspace/<sub> 约定路径映射到环境 workspace 的 WSL 路径（ntpath 反斜杠形态同映射）。"""
    from process_manager import _map_wsl_working_dir

    backend = {"workspace_wsl": "/mnt/d/ws"}
    assert _map_wsl_working_dir(backend, "/workspace/out.txt") == "/mnt/d/ws/out.txt"
    assert _map_wsl_working_dir(backend, "\\workspace\\logs\\a.log") == "/mnt/d/ws/logs/a.log"
