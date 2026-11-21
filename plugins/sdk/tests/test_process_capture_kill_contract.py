# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""SDK 进程运行时「捕获与击杀契约」测试（ADR 2026-11-07 决策 2/3/4/6）。

背景实证（da6e0bb9b1df，pid 53288）：长编译链 290s 超时转后台 26 秒后被
terminate（exit 9），日志仅 header 零输出——捕获面落相对路径 + 击杀不 flush +
终止行伪装 user。本文件钉四条契约：

1. 捕获面 = 按会话键的绝对路径持久日志：退出不删（正常退出宽限删除机制退役），
   7 天过期清扫保留且只扫孤儿（含会话子目录）；
2. 击杀全路径（terminate / watchdog）先排空已捕获输出再写终止行——被杀进程
   日志必须含全部已产生输出（53288 场景根修）；
3. 终止原因如实：terminate reason 透传（缺省 user 保持现行为），watchdog 终止行
   带具体原因（orphan / unit_memory / memory_pressure）；
4. 孤儿击杀判据 = 无输出时长（有持续输出的长任务不被孤儿杀；无输出挂死仍按
   原超时杀；内存压力击杀保留）。

测试桩纪律：外部依赖（asyncio 进程 / ProcessBackend）用最小桩；53288 回归与
正常退出持久性用真实子进程（关键路径至少一条真实依赖）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.process_runtime import (
    ProcessInfo,
    ProcessManager,
)
from agentos_plugin_sdk.process_runtime.backends import _get_local_backend

pytestmark = pytest.mark.unit


# ─────────────────────────── 桩与助手 ───────────────────────────


class _StubProcess:
    """最小 asyncio 进程桩：真实 StreamReader 承载 reader 任务，wait 可编程。

    die() 模拟进程被杀后的真实序列：管道 EOF（read 返回余量后到头）+ wait 放行。
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin: Any = None
        self.returncode: int | None = None
        self._wait_event = asyncio.Event()
        self._exit_code_on_wait = 0

    def emit(self, data: bytes) -> None:
        self.stdout.feed_data(data)

    def die(self, exit_code: int) -> None:
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = exit_code
        self._exit_code_on_wait = exit_code
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        return self._exit_code_on_wait


class _KillEofBackend:
    """kill 桩：记录调用并让桩进程死亡（EOF），模拟真实 kill 后的排空序列。"""

    def __init__(self, proc: _StubProcess) -> None:
        self.proc = proc
        self.killed: list[int] = []

    async def kill(self, unit: Any, force: bool = True) -> None:
        self.killed.append(unit.pid)
        self.proc.die(-9 if force else -15)

    async def sample_memory(self) -> float | None:
        return None

    async def sample_unit_memory(self, unit: Any) -> int | None:
        return None


class _RecordKillBackend:
    """纯记录 kill 的假后端（孤儿判据策略测试用）。"""

    def __init__(self) -> None:
        self.killed: list[int] = []

    async def kill(self, unit: Any, force: bool = True) -> None:
        self.killed.append(unit.pid)

    async def sample_memory(self) -> float | None:
        return None

    async def sample_unit_memory(self, unit: Any) -> int | None:
        return None


def _register(
    mgr: ProcessManager,
    proc: Any,
    *,
    command: str = "sleep 999",
    log_dir: Path | None = None,
    owner: str | None = None,
    backend: Any = None,
) -> tuple[int, Path]:
    """经 _register_process 挂桩进程（绕过 shell 路由，被测面是注册与读写契约）。

    log_dir 缺省时按 start_process 同款接线：根目录经 _session_log_dir 按会话
    键归位。
    """
    effective_dir = log_dir if log_dir is not None else mgr._session_log_dir(mgr.log_dir, owner)
    pid, log_file = mgr._register_process(
        command=command,
        process=proc,
        session_log_dir=effective_dir,
        on_output=None,
        backend=backend,
        metadata={},
        owner=owner,
    )
    return pid, log_file


async def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    """轮询等待条件成立（有界，防挂死）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _make_info(
    pid: int,
    *,
    last_access: float,
    last_output: float,
    start: float | None = None,
    backend: Any = None,
) -> ProcessInfo:
    return ProcessInfo(
        pid=pid,
        command="cargo build",
        start_time=start if start is not None else time.time() - 1000,
        log_file=Path("unused.log"),
        process=None,
        status="running",
        backend=backend,
        last_access_time=last_access,
        last_output_time=last_output,
    )


# ─────────────────────── 契约 1：绝对路径根常量 ───────────────────────


class TestDefaultLogRoot:
    def test_default_root_is_absolute_cwd_anchored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无项目根注入时缺省根 = 构造时刻 CWD 锚定的 data/bash_logs（绝对路径）。

        假设：sidecar 生命周期内 CWD 稳定，构造时刻一次解析、不随命令 CWD 漂移。
        """
        monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
        mgr = ProcessManager()
        assert mgr.log_dir.is_absolute(), "缺省日志根必须为绝对路径（原缺陷：相对 logs/bash 落 sidecar CWD）"
        assert mgr.log_dir == Path.cwd() / "data" / "bash_logs"

    def test_default_root_anchored_by_project_root_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_PROJECT_ROOT 注入时缺省根锚定项目根（SDK 既有项目根注入面）。"""
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path))
        mgr = ProcessManager()
        assert mgr.log_dir == tmp_path / "data" / "bash_logs"
        assert mgr.log_dir.is_absolute()

    def test_explicit_log_dir_resolved_absolute(self, tmp_path: Path) -> None:
        """显式传入 log_dir 也归一化为绝对路径（对上契约不因调用方形态漂移）。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        assert mgr.log_dir.is_absolute()
        assert mgr.log_dir == (tmp_path / "root").resolve()


# ─────────────────────── 契约 1：按会话键的日志布局 ───────────────────────


class TestSessionKeyLayout:
    def test_session_key_sanitized(self) -> None:
        """owner → 会话键目录名：非法字符替换、空值/点串退化 None、超长截断。"""
        assert ProcessManager._session_key("pipeline abc/1") == "pipeline_abc_1"
        assert ProcessManager._session_key(None) is None
        assert ProcessManager._session_key("") is None
        assert ProcessManager._session_key("..") is None
        assert len(ProcessManager._session_key("x" * 500)) == 80

    def test_missing_key_degrades_to_default_dir(self, tmp_path: Path) -> None:
        """会话键缺失退化到缺省目录（日志根本身）。

        假设：键缺失退化 = 根目录平铺（与历史布局同形，read_log_by_pid 根目录
        直查即覆盖），不另设 default 子目录。
        """
        mgr = ProcessManager(log_dir=tmp_path / "root")
        assert mgr._session_log_dir(mgr.log_dir, None) == mgr.log_dir
        assert mgr._session_log_dir(mgr.log_dir, "sess-1") == mgr.log_dir / "sess-1"

    @pytest.mark.asyncio
    async def test_spawn_with_owner_logs_under_session_dir(self, tmp_path: Path) -> None:
        """带 owner 的进程日志落 <根>/<会话键>/bash_<pid>.log，头部保留原始 owner。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7001)
        pid, log_file = _register(mgr, proc, owner="sess A/1")
        assert log_file.parent == tmp_path / "root" / "sess_A_1"
        assert log_file.name == f"bash_{pid}.log"
        proc.die(0)
        await _wait_until(lambda: pid not in mgr.active_processes)
        text = log_file.read_text(encoding="utf-8")
        assert "# Owner: sess A/1" in text

    def test_read_log_by_pid_finds_session_subdir_log(self, tmp_path: Path) -> None:
        """read_log_by_pid 跨会话子目录定位（agent 手里只有 pid）。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        sess = tmp_path / "root" / "sessA"
        sess.mkdir(parents=True)
        (sess / "bash_8100.log").write_text(
            "# Bash Command Log\n# Command: echo hi\n# PID: 8100\n"
            "# Started: 2026-01-01T00:00:00\n# Platform: Windows\n"
            f"# {'=' * 50}\nhi-out\n",
            encoding="utf-8",
        )
        data = mgr.read_log_by_pid(8100)
        assert data is not None
        assert data["output"] == "hi-out"
        assert data["command"] == "echo hi"

    def test_read_log_by_pid_flat_layout_still_readable(self, tmp_path: Path) -> None:
        """键缺失退化布局（根目录平铺）与历史数据可读。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        (tmp_path / "root" / "bash_8101.log").write_text(
            "# Bash Command Log\n# Command: ls\n# PID: 8101\n"
            f"# {'=' * 50}\nflat-out\n",
            encoding="utf-8",
        )
        data = mgr.read_log_by_pid(8101)
        assert data is not None and data["output"] == "flat-out"

    def test_read_log_by_pid_missing_returns_none(self, tmp_path: Path) -> None:
        mgr = ProcessManager(log_dir=tmp_path / "root")
        assert mgr.read_log_by_pid(99999) is None


# ─────────────────────── 契约 1：正常退出不删日志 ───────────────────────


class TestNormalExitPersistence:
    @pytest.mark.asyncio
    async def test_completed_process_log_kept_no_grace_delete(self, tmp_path: Path) -> None:
        """正常退出（rc==0）日志持久保留——宽限删除机制退役，看门狗 tick 不删。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('done-output')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        pid, log_file = mgr._register_process(
            command="echo done", process=proc, session_log_dir=mgr.log_dir,
            on_output=None, backend=_get_local_backend(), metadata={},
        )
        await _wait_until(lambda: pid not in mgr.active_processes)
        assert log_file.exists(), "正常退出日志必须保留（capture 契约底线：任何时候都能读）"
        data = mgr.read_log_by_pid(pid)
        assert data is not None
        assert "done-output" in data["output"]
        assert data["exit_code"] == 0
        # 看门狗 tick（原宽限删除的 flush 挂载点）后仍在
        await mgr._watchdog_check_once()
        assert log_file.exists()

    def test_grace_delete_mechanism_retired(self, tmp_path: Path) -> None:
        """宽限删除机制的属性/方法应已移除（清理原则，不并存两套）。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        assert not hasattr(mgr, "_logs_pending_delete")
        assert not hasattr(mgr, "flush_expired_log_deletes")
        assert not hasattr(mgr, "_unlink_command_log")
        assert not hasattr(mgr, "_LOG_DELETE_GRACE_SECONDS_DEFAULT")

    def test_startup_sweep_covers_session_subdirs(self, tmp_path: Path) -> None:
        """7 天过期清扫只扫孤儿：会话子目录内过期日志删、新日志与非 bash_ 前缀不动。"""
        root = tmp_path / "root"
        sess = root / "sessA"
        sess.mkdir(parents=True)
        stale = sess / "bash_1.log"
        stale.write_text("old", encoding="utf-8")
        old = time.time() - 8 * 24 * 3600.0
        os.utime(stale, (old, old))
        fresh = sess / "bash_2.log"
        fresh.write_text("new", encoding="utf-8")
        other = sess / "other_3.log"
        other.write_text("keep", encoding="utf-8")
        os.utime(other, (old, old))

        ProcessManager(log_dir=root)  # 构造即清扫

        assert not stale.exists(), "会话子目录内过期孤儿日志应被清扫"
        assert fresh.exists()
        assert other.exists(), "非 bash_ 前缀不在清扫范围"


# ─────────────────────── 契约 2+3：击杀前 flush 与真实原因 ───────────────────────


class TestKillFlushAndReason:
    @pytest.mark.asyncio
    async def test_terminate_drains_captured_output_before_line(self, tmp_path: Path) -> None:
        """击杀后先排空已捕获输出再写终止行：输出在前、终止行收尾。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7101)
        backend = _KillEofBackend(proc)
        pid, log_file = _register(mgr, proc, backend=backend)
        proc.emit(b"captured-1\ncaptured-2\n")
        assert await _wait_until(
            lambda: "captured-2" in log_file.read_text(encoding="utf-8")
        ), "前置：已捕获输出先落盘"

        ok, err = await mgr.terminate_process(pid)

        assert (ok, err) == (True, None)
        assert backend.killed == [pid]
        lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert "captured-1" in lines and "captured-2" in lines, "已产生输出必须全部在日志"
        assert lines[-1] == "# Process terminated by user", "缺省 reason=user 保持现行为"

    @pytest.mark.asyncio
    async def test_terminate_reason_passthrough(self, tmp_path: Path) -> None:
        """terminate reason 透传：真实调用方身份写终止行（不再统一伪装 user）。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7102)
        pid, log_file = _register(mgr, proc, backend=_KillEofBackend(proc))
        proc.emit(b"partial-work\n")

        ok, _ = await mgr.terminate_process(pid, force=True, reason="self_heal")

        assert ok is True
        lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines[-1] == "# Process terminated by self_heal"

    @pytest.mark.asyncio
    async def test_terminate_reason_newline_sanitized(self, tmp_path: Path) -> None:
        """reason 内换行清洗为空格（终止行单行契约，防日志结构注入）。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7103)
        pid, log_file = _register(mgr, proc, backend=_KillEofBackend(proc))

        ok, _ = await mgr.terminate_process(pid, reason="watchdog\ninjected line")

        assert ok is True
        assert "# Process terminated by watchdog injected line" in log_file.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_watchdog_backend_kill_line_carries_concrete_reason(self, tmp_path: Path) -> None:
        """watchdog 击杀行带具体原因（unit_memory），且输出排空在前。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7104)
        backend = _KillEofBackend(proc)
        pid, log_file = _register(mgr, proc, backend=backend)
        proc.emit(b"before-kill\n")
        assert await _wait_until(lambda: "before-kill" in log_file.read_text(encoding="utf-8"))

        info = mgr.active_processes[pid]
        await mgr._watchdog_kill(pid, info, "unit_memory")

        lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert "before-kill" in lines
        assert lines[-1] == "# Process terminated by watchdog (unit_memory)"

    @pytest.mark.asyncio
    async def test_watchdog_fallback_kill_line_carries_reason(self, tmp_path: Path) -> None:
        """无 backend 的看门狗回退 terminate_process：终止行仍是 watchdog+具体原因。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7105)
        pid, log_file = _register(mgr, proc, backend=None)

        await mgr._watchdog_kill(pid, mgr.active_processes[pid], "orphan")

        lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines[-1] == "# Process terminated by watchdog (orphan)"

    @pytest.mark.asyncio
    async def test_shutdown_all_terminate_line_honest(self, tmp_path: Path) -> None:
        """shutdown_all 击杀终止行如实（shutdown），不伪装 user。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7106)
        pid, log_file = _register(mgr, proc, backend=_KillEofBackend(proc))

        killed = await mgr.shutdown_all()

        assert killed == 1
        assert "# Process terminated by shutdown" in log_file.read_text(encoding="utf-8")


# ─────────────────────── 契约 4：孤儿判据 = 无输出时长 ───────────────────────


class TestOrphanCriterion:
    @pytest.mark.asyncio
    async def test_outputting_long_task_not_orphan_killed(self, tmp_path: Path) -> None:
        """有持续输出的长任务（cargo build 形态）不被孤儿杀——即使久无访问。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        mgr._orphan_timeout = 60.0
        backend = _RecordKillBackend()
        now = time.time()
        mgr.active_processes[401] = _make_info(
            401, last_access=now - 600, last_output=now - 1.0, backend=backend
        )

        await mgr._watchdog_check_once()

        assert backend.killed == [], "输出 1s 前仍在产出的长任务不得被孤儿判据杀"

    @pytest.mark.asyncio
    async def test_output_silent_hang_still_killed_despite_access(self, tmp_path: Path) -> None:
        """无输出挂死仍按原超时杀——即使 agent 一直轮询（访问新鲜）。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        mgr._orphan_timeout = 60.0
        backend = _RecordKillBackend()
        now = time.time()
        mgr.active_processes[402] = _make_info(
            402, last_access=now - 1.0, last_output=now - 600, backend=backend
        )

        await mgr._watchdog_check_once()

        assert 402 in backend.killed, "无输出超阈值的挂死进程必须杀"

    @pytest.mark.asyncio
    async def test_never_output_counts_from_start(self, tmp_path: Path) -> None:
        """从未产出过输出（last_output_time=0）→ 无输出时长从启动时刻起算。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        mgr._orphan_timeout = 60.0
        backend = _RecordKillBackend()
        now = time.time()
        mgr.active_processes[403] = _make_info(
            403, last_access=now - 1.0, last_output=0.0, start=now - 600, backend=backend
        )

        await mgr._watchdog_check_once()

        assert 403 in backend.killed

    @pytest.mark.asyncio
    async def test_output_silence_within_threshold_not_killed(self, tmp_path: Path) -> None:
        """无输出但未超阈值 → 不杀（≥2 组区分输入：阈值边界另一侧）。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        mgr._orphan_timeout = 60.0
        backend = _RecordKillBackend()
        now = time.time()
        mgr.active_processes[404] = _make_info(
            404, last_access=now - 1.0, last_output=now - 30, backend=backend
        )

        await mgr._watchdog_check_once()

        assert backend.killed == []


# ─────────────────────── 契约 2：53288 场景回归（真实子进程） ───────────────────────


class Test53288Regression:
    @pytest.mark.asyncio
    async def test_killed_long_task_log_complete_via_read_log_by_pid(self, tmp_path: Path) -> None:
        """长任务被击杀后 read_log_by_pid 完整取回 header + 已产生输出 + 真实终止行。

        原缺陷（pid 53288）：超时转后台 26 秒后被 terminate（exit 9），日志仅
        header 零输出，agent 轮询取回空壳 → 整段重跑。根修 = 击杀前已捕获输出
        全部落盘 + 终止行如实，read_log_by_pid（磁盘真理源）完整可取。
        """
        mgr = ProcessManager(log_dir=tmp_path / "root")
        child = (
            "import time\n"
            "for i in range(1000):\n"
            "    print(f'compile-line-{i}', flush=True)\n"
            "    time.sleep(0.01)\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", child,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        pid, log_file = mgr._register_process(
            command="cargo build (simulated)", process=proc, session_log_dir=mgr.log_dir,
            on_output=None, backend=_get_local_backend(), metadata={},
        )
        # 前置：被杀前已产生输出并落盘（capture 契约的半程）
        pre_kill: list[str] = []

        def _captured() -> list[str]:
            data = mgr.read_log_by_pid(pid)
            return [ln for ln in (data["output"].splitlines() if data else []) if ln.startswith("compile-line-")]

        assert await _wait_until(lambda: len(_captured()) >= 10, timeout=15.0), "前置：被杀前输出应已落盘"
        pre_kill = _captured()
        assert len(pre_kill) >= 10

        ok, err = await mgr.terminate_process(pid, force=True, reason="timeout_ceiling")

        assert (ok, err) == (True, None)
        assert log_file.exists(), "被杀进程日志持久保留"
        data = mgr.read_log_by_pid(pid)
        assert data is not None, "被杀后凭 pid 磁盘可读（log 契约底线）"
        assert data["command"] == "cargo build (simulated)", "header（命令）完整可取"
        assert data["owner"] is None
        assert data["exit_code"] is not None, "退出码从日志尾解析可取"
        kept = [ln for ln in data["output"].splitlines() if ln.startswith("compile-line-")]
        assert kept == pre_kill or len(kept) >= len(pre_kill), "已产生输出零丢失"
        for line in pre_kill:
            assert line in kept
        assert data["terminated_by"] == "timeout_ceiling", "真实终止行可取（不再伪装 user）"

    @pytest.mark.asyncio
    async def test_reap_after_kill_records_exit_code_and_reason(self, tmp_path: Path) -> None:
        """watchdog backend 路径收尾：exit_code 落 info、日志含 watchdog+原因。"""
        mgr = ProcessManager(log_dir=tmp_path / "root")
        proc = _StubProcess(7201)
        pid, log_file = _register(mgr, proc, backend=_KillEofBackend(proc))
        info = mgr.active_processes[pid]
        proc.die(-9)

        await mgr._reap_after_kill(pid, info, "memory_pressure")

        assert info.status == "terminated"
        assert info.exit_code == -9
        text = log_file.read_text(encoding="utf-8")
        assert "# Process terminated by watchdog (memory_pressure)" in text
