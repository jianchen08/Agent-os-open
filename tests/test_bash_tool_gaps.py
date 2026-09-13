# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""bash 工具缺口补测（2026-09-13 官方车道 tool.py miss=30）。

覆盖缺口：
- SecurityChecker：allowed_commands 白名单拒绝分支、CAUTION 降级、
  手动后台化降级；
- _compact_result_data：信号终止附加 terminated_by_signal；
- _check_owner：无身份调用 / 无归属进程两拒绝分支；
- execute 分发：未知 action、command 缺失；
- execute 执行链：日志不可写时的 SUMMARY_ERROR、工作目录不存在时的
  EXECUTION_FAILED（真实子进程 spawn 失败）；
- continue：磁盘日志 IO 故障、磁盘日志越权、内存完成态非零退出、
  等待轮询完成后非零/零退出（真实子进程）；
- terminate/input/read_log：MISSING_PID / MISSING_INPUT / PROCESS_NOT_FOUND、
  input 越权与禁字输入失败（真实子进程）、read_log 磁盘 IO 故障。

打桩边界：无——子进程全部真实拉起（python -c 短命/定时退出进程），
文件操作落 tmp_path；owner 越权经 session_id 注入参数走真实校验链。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASH_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "bash"


def _evict_foreign_bare_modules() -> None:
    """逐出指向 bash 目录之外的裸名缓存（tool 等为多插件同名裸模块）。"""
    for name in (
        "tool",
        "bash_types",
        "process_manager",
        "encoding",
        "input_handler",
        "log_compressor",
        "result_types",
    ):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        mod_file = getattr(mod, "__file__", None)
        if mod_file and Path(mod_file).resolve() == (_BASH_DIR / f"{name}.py").resolve():
            continue
        sys.modules.pop(name, None)


_evict_foreign_bare_modules()
if str(_BASH_DIR) not in sys.path or sys.path[0] != str(_BASH_DIR):
    sys.path.insert(0, str(_BASH_DIR))

from bash_types import ProcessInfo  # noqa: E402
from process_manager import ProcessManager  # noqa: E402
from tool import BashTool, SecurityChecker, describe_exit_code  # noqa: E402

pytestmark = pytest.mark.unit


def _py_cmd(code: str) -> str:
    """跨 shell（WSL/MSYS bash/CMD）安全的 python 单行命令。"""
    return f'"{Path(sys.executable).as_posix()}" -c "{code}"'


def _sleep_exit(seconds: float, code: int) -> str:
    return _py_cmd(f"import time; time.sleep({seconds}); import sys; sys.exit({code})")


def _write_log(log_dir: Path, pid: int, command: str, owner: str | None, body: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"bash_{pid}.log"
    lines = [
        "# Bash Command Log",
        f"# Command: {command}",
        f"# PID: {pid}",
    ]
    if owner is not None:
        lines.append(f"# Owner: {owner}")
    lines += [
        "# Started: 2026-01-01T00:00:00",
        "# Platform: Linux",
        "# ==================================================",
        "",
    ]
    log_file.write_text("\n".join(lines) + body, encoding="utf-8")
    return log_file


@pytest.fixture
async def tool(tmp_path, monkeypatch):
    """BashTool + 落 tmp 的 ProcessManager（chdir 隔离默认相对 log_dir 副作用）。"""
    monkeypatch.chdir(tmp_path)
    t = BashTool()
    t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    yield t
    await t.process_manager.shutdown_all()


# ───────────────────────────── SecurityChecker ─────────────────────────────


class TestSecurityCheckerAllowedList:
    def _checker(self) -> SecurityChecker:
        return SecurityChecker(allowed_commands=["ls", "echo"])

    @pytest.mark.parametrize(
        ("command", "base"),
        [("python -c 'import os'", "python"), ("curl https://example.com", "curl"), ("", "")],
    )
    def test_command_outside_allowlist_rejected(self, command, base):
        safe, warning, message = self._checker().check(command)
        assert safe is False
        assert warning is False
        assert f"命令不在允许列表中: {base}" in message

    def test_allowed_command_passes_through(self):
        safe, warning, message = self._checker().check("ls -la")
        assert (safe, warning, message) == (True, False, None)

    def test_no_allowlist_skips_gate(self):
        safe, warning, message = SecurityChecker().check("python -c 'print(1)'")
        assert safe is True and warning is False and message is None


class TestSecurityCheckerCautionAndBackground:
    @pytest.mark.parametrize(
        "command",
        ["curl https://example.com/data", "wget -O out.bin https://example.com", "echo hi > out.txt"],
    )
    def test_caution_commands_downgraded_to_warning(self, command):
        safe, warning, message = SecurityChecker().check(command)
        assert safe is True
        assert warning is True
        assert "命令包含潜在风险操作" in message

    @pytest.mark.parametrize(
        "command",
        ["nohup python server.py", "setsid python server.py", "echo hi &"],
    )
    def test_manual_background_downgraded_to_warning(self, command):
        safe, warning, message = SecurityChecker().check(command)
        assert safe is True
        assert warning is True
        assert "手动后台化" in message

    def test_dangerous_still_hard_blocked_before_caution(self):
        safe, warning, message = SecurityChecker(allowed_commands=["rm"]).check("rm -rf /")
        assert safe is False
        assert "危险操作" in message


# ───────────────────────────── 结果精简与 owner 校验 ─────────────────────────────


class TestCompactResultDataSignal:
    @pytest.mark.parametrize(
        ("exit_code", "expected"),
        [(137, "信号 9 (SIGKILL) 终止"), (143, "信号 15 (SIGTERM) 终止"), (129, "信号 1 (SIGHUP) 终止")],
    )
    def test_signal_exit_appends_terminated_by_signal(self, exit_code, expected):
        data = BashTool._compact_result_data(pid=7, output="out", summary_obj={}, exit_code=exit_code)
        assert data["terminated_by_signal"] == expected
        assert describe_exit_code(exit_code) == expected

    @pytest.mark.parametrize("exit_code", [0, 1, 124, None])
    def test_non_signal_exit_has_no_signal_field(self, exit_code):
        data = BashTool._compact_result_data(pid=7, output="out", summary_obj={}, exit_code=exit_code)
        assert "terminated_by_signal" not in data


class TestCheckOwner:
    @pytest.mark.parametrize(
        ("caller", "proc", "expect_ok", "fragment"),
        [
            (None, "sess-a", False, "无身份调用被拒绝"),
            ("sess-b", None, False, "无归属会话"),
            ("sess-b", "sess-a", False, "越权操作被拒绝"),
            (None, None, True, ""),
            ("sess-a", "sess-a", True, ""),
        ],
    )
    def test_owner_matrix(self, caller, proc, expect_ok, fragment):
        ok, err = BashTool._check_owner(42, caller, proc)
        assert ok is expect_ok
        if fragment:
            assert fragment in err


# ───────────────────────────── execute 分发 ─────────────────────────────


class TestExecuteDispatch:
    async def test_unknown_action_rejected(self, tool):
        result = await tool.execute({"action": "reboot", "pid": "not-an-int"})
        assert result.success is False
        assert result.error_code == "INVALID_ACTION"
        assert "未知的 action" in result.error

    async def test_execute_without_command_rejected(self, tool):
        result = await tool.execute({"action": "execute"})
        assert result.success is False
        assert result.error_code == "MISSING_COMMAND"


# ───────────────────────────── execute 执行链 ─────────────────────────────


class TestExecuteFailurePaths:
    async def test_unwritable_log_dir_reports_summary_error(self, tool, tmp_path):
        """日志链路不可写 + 进程已清：摘要无从获取 → SUMMARY_ERROR（628 行）。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await tool.execute(
            {
                "command": _py_cmd("print('ignored')"),
                "workspace": str(ws),
                "project_root": str(blocker),  # 日志目录父级是文件 → 永不落盘
                "timeout": 20,
            }
        )

        assert result.success is False
        assert result.error_code == "SUMMARY_ERROR"

    async def test_spawn_failure_reports_execution_failed(self, tool, tmp_path):
        """working_dir 不存在：spawn 抛错被收口为 EXECUTION_FAILED（633-634 行）。"""
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await tool.execute(
            {
                "command": "echo hi",
                "workspace": str(ws),
                "working_dir": str(tmp_path / "no-such-dir"),
            }
        )

        assert result.success is False
        assert result.error_code == "EXECUTION_FAILED"
        assert "执行命令失败" in result.error


# ───────────────────────────── continue 各分支 ─────────────────────────────


class TestContinuePaths:
    async def test_log_io_error_surfaced(self, tool):
        """磁盘日志存在但读不出（目录占位）→ LOG_FILE_IO_ERROR（658-659 行）。"""
        (tool.process_manager.log_dir / "bash_400.log").mkdir()

        result = await tool.execute({"action": "continue", "pid": 400})

        assert result.success is False
        assert result.error_code == "LOG_FILE_IO_ERROR"

    async def test_disk_log_owner_mismatch_rejected(self, tool):
        """磁盘日志归属他 会话：continue 越权被拒（666 行）。"""
        _write_log(tool.process_manager.log_dir, 401, "echo hi", "sess-a", "hi\n")

        result = await tool.execute(
            {"action": "continue", "pid": 401, "session_id": "sess-b"}
        )

        assert result.success is False
        assert result.error_code == "PROCESS_FORBIDDEN"

    async def test_disk_log_owner_match_allowed(self, tool):
        """同会话磁盘降级：success + source=file（对照组）。"""
        _write_log(tool.process_manager.log_dir, 402, "echo hi", "sess-a", "hi\n")

        result = await tool.execute(
            {"action": "continue", "pid": 402, "session_id": "sess-a"}
        )

        assert result.success is True
        assert result.metadata["source"] == "file"
        assert result.output["output"] == "hi"

    async def test_memory_completed_nonzero_exit_fails(self, tool):
        """内存完成态 + 非零退出：COMMAND_FAILED 带失败摘要（696-700 行）。"""
        log_file = _write_log(tool.process_manager.log_dir, 310, "failing", None, "boom\n")
        tool.process_manager.active_processes[310] = ProcessInfo(
            pid=310,
            command="failing",
            start_time=time.time(),
            log_file=log_file,
            status="completed",
            exit_code=3,
        )

        result = await tool.execute({"action": "continue", "pid": 310, "timeout": 1})

        assert result.success is False
        assert result.error_code == "COMMAND_FAILED"
        assert "退出码: 3" in result.error
        assert result.metadata["exit_code"] == 3

    async def test_memory_completed_signal_exit_reports_signal(self, tool):
        """内存完成态 + 信号退出：失败消息与 metadata 带信号描述（对照组）。"""
        log_file = _write_log(tool.process_manager.log_dir, 311, "killed", None, "part\n")
        tool.process_manager.active_processes[311] = ProcessInfo(
            pid=311,
            command="killed",
            start_time=time.time(),
            log_file=log_file,
            status="error",
            exit_code=137,
        )

        result = await tool.execute({"action": "continue", "pid": 311, "timeout": 1})

        assert result.success is False
        assert "信号 9 (SIGKILL) 终止" in result.error
        assert result.metadata["terminated_by_signal"] == "信号 9 (SIGKILL) 终止"

    async def test_wait_loop_completion_nonzero_exit_fails(self, tool, tmp_path):
        """等待轮询期间进程失败退出：COMMAND_FAILED（755-759 行）。

        注入真实子进程 + 不挂日志读取任务：真实链路里 _on_output_task_done
        的即时清理与轮询循环存在竞态（清理先到时 proc_info 被弹出，退出码
        落到磁盘降级路径），不挂读取任务即钉住循环内的非 running 迁移，
        使退出码经内存路径进入失败分支。
        """
        ws = tmp_path / "ws"
        ws.mkdir()
        log_file = _write_log(tool.process_manager.log_dir, 320, "delayed-fail", None, "some output\n")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(0.6); import sys; sys.exit(5)",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        tool.process_manager.active_processes[proc.pid] = ProcessInfo(
            pid=proc.pid,
            command="delayed-fail",
            start_time=time.time(),
            log_file=log_file,
            process=proc,
            status="running",
            metadata={"owner": "sess-a"},
        )
        try:
            result = await tool.execute(
                {"action": "continue", "pid": proc.pid, "timeout": 10, "session_id": "sess-a"}
            )

            assert result.success is False
            assert result.error_code == "COMMAND_FAILED"
            assert "退出码: 5" in result.error
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def test_wait_loop_completion_success_returns_output(self, tool, tmp_path):
        """等待轮询期间进程正常退出：success + output（755 行的零退出对照组）。"""
        ws = tmp_path / "ws"
        ws.mkdir()

        started = await tool.execute(
            {"command": _sleep_exit(2.0, 0), "workspace": str(ws), "timeout": 1}
        )
        assert started.output["status"] == "running"
        pid = started.output["pid"]

        result = await tool.execute(
            {"action": "continue", "pid": pid, "timeout": 10, "workspace": str(ws)}
        )

        assert result.success is True
        assert result.output["status"] == "completed"
        assert result.output["exit_code"] == 0


# ───────────────────────────── terminate / input / read_log ─────────────────────────────


class TestTerminateAndInputGuards:
    async def test_terminate_without_pid_rejected(self, tool):
        result = await tool.execute({"action": "terminate"})
        assert result.success is False
        assert result.error_code == "MISSING_PID"

    async def test_input_without_pid_rejected(self, tool):
        result = await tool.execute({"action": "input"})
        assert result.success is False
        assert result.error_code == "MISSING_PID"

    async def test_input_without_text_rejected(self, tool):
        result = await tool.execute({"action": "input", "pid": 424242})
        assert result.success is False
        assert result.error_code == "MISSING_INPUT"

    async def test_input_unknown_process_rejected(self, tool):
        result = await tool.execute({"action": "input", "pid": 424242, "input_text": "hi"})
        assert result.success is False
        assert result.error_code == "PROCESS_NOT_FOUND"

    async def test_input_owner_mismatch_rejected(self, tool, tmp_path):
        """运行中进程归属他会话：input 越权被拒（849 行，真实子进程）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        started = await tool.execute(
            {"command": _sleep_exit(5, 0), "workspace": str(ws), "session_id": "sess-a", "timeout": 1}
        )
        assert started.output["status"] == "running"
        pid = started.output["pid"]

        result = await tool.execute(
            {"action": "input", "pid": pid, "input_text": "hello", "session_id": "sess-b"}
        )

        assert result.success is False
        assert result.error_code == "PROCESS_FORBIDDEN"

    async def test_input_forbidden_char_fails_send(self, tool, tmp_path):
        """运行中进程收到禁字输入：INPUT_FAILED（862 行，真实子进程）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        started = await tool.execute(
            {"command": _sleep_exit(5, 0), "workspace": str(ws), "session_id": "sess-a", "timeout": 1}
        )
        assert started.output["status"] == "running"
        pid = started.output["pid"]

        result = await tool.execute(
            {"action": "input", "pid": pid, "input_text": "bad\x00input", "session_id": "sess-a"}
        )

        assert result.success is False
        assert result.error_code == "INPUT_FAILED"
        assert "禁止字符" in result.error

    async def test_input_owner_match_succeeds(self, tool, tmp_path):
        """同会话向运行中进程发送输入：success（849 行对照组）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        started = await tool.execute(
            {"command": _sleep_exit(5, 0), "workspace": str(ws), "session_id": "sess-a", "timeout": 1}
        )
        pid = started.output["pid"]

        result = await tool.execute(
            {"action": "input", "pid": pid, "input_text": "hello", "session_id": "sess-a"}
        )

        assert result.success is True
        assert result.output["message"] == "输入已发送"


class TestReadLogPaths:
    async def test_read_log_io_error_surfaced(self, tool):
        """磁盘日志存在但读不出（目录占位）→ LOG_FILE_IO_ERROR（918-919 行）。"""
        (tool.process_manager.log_dir / "bash_500.log").mkdir()

        result = await tool.execute({"action": "read_log", "pid": 500})

        assert result.success is False
        assert result.error_code == "LOG_FILE_IO_ERROR"

    async def test_read_log_owner_mismatch_rejected(self, tool):
        """磁盘日志归属他会话：read_log 越权被拒（对照链路）。"""
        _write_log(tool.process_manager.log_dir, 501, "echo hi", "sess-a", "hi\n")

        result = await tool.execute({"action": "read_log", "pid": 501, "session_id": "sess-b"})

        assert result.success is False
        assert result.error_code == "PROCESS_FORBIDDEN"
