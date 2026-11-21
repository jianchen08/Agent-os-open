# @feature: FP-0.2.spill_guard 四工具契约 | @ci: python-coverage
"""bash 工具面四单词重构契约测试（ADR 2026-11-07 决策 1/3/4，T2 Red 先行）。

七条验收判据（验收契约，实现落盘前 Red）：
a) bash 前台完成返回完整输出（handle 取代 pid）
b) 前台超时返回 running+handle+全部已捕获输出，进程不死，log(block=true)
   后续能等到完成（决策 3 用户裁定修正版语义）
c) background=true 立即返回 handle
d) log block 两态：block=false 立即读当前态/完整日志（含进程已清后的磁盘路径，
   T1 持久日志契约贯通）；block=true 等到完成或等待上限
e) input 向等待输入的进程发文本生效
f) kill 后 log 仍可读完整输出（T1 击杀前 flush 契约贯通，终止原因 = agent）
g) 越权 handle 拒绝（owner 校验沿用，pid 复用防劫持双重校验）

外加：handle 编解码契约（不透明串 = pid+owner+校验和，畸形/篡改拒绝）、
MAX_TIMEOUT=600、超长 timeout 钳制、参数缺失错误码。

外部依赖最小桩：无——进程全部真实拉起（python -c），log_dir 指 tmp_path；
仅 Windows 无 bash/wsl 时跳过（ProcessManager 的 shell 选择依赖）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from tool import BashTool, decode_handle, encode_handle

from agentos_plugin_sdk.process_runtime import ProcessInfo, ProcessManager

pytestmark = pytest.mark.unit

_NEEDS_SHELL = pytest.mark.skipif(
    sys.platform == "win32" and not (shutil.which("bash") or shutil.which("wsl")),
    reason="Windows 下需要 bash/wsl（ProcessManager 的 shell 选择）",
)


def _craft_handle(payload: object) -> str:
    """以合法校验和构造任意 payload 的 handle（触发 decode 深层分支）。"""
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    digest = hashlib.sha256(body.encode("ascii")).hexdigest()[:8]
    return f"bs1.{body}.{digest}"


def _py_cmd(code: str) -> str:
    return f'"{Path(sys.executable).as_posix()}" -c "{code}"'


def _make_tool(tmp_path: Path) -> BashTool:
    t = BashTool()
    t.process_manager = ProcessManager(log_dir=tmp_path / "logs")
    return t


def _inject_live_process(tool: BashTool, pid: int, owner: str) -> None:
    """向 ProcessManager 注入一条活进程记录（metadata owner 指定归属）。"""
    log_file = tool.process_manager.log_dir / f"bash_{pid}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("out\n", encoding="utf-8")
    tool.process_manager.active_processes[pid] = ProcessInfo(
        pid=pid,
        command="echo hi",
        start_time=time.time(),
        log_file=log_file,
        status="running",
        metadata={"owner": owner},
    )


# ══════════════════════════════════════════════════════════════════
# handle 编解码契约（不透明串：pid + owner + 校验和）
# ══════════════════════════════════════════════════════════════════


class TestHandleCodec:
    def test_encode_decode_roundtrip(self) -> None:
        """编码→解码还原 pid + owner（含 owner=None 双向）。"""
        for owner in ("sess-abc", None, "workspace/中文/会话"):
            handle = encode_handle(12345, owner)
            decoded = decode_handle(handle)
            assert decoded is not None, f"owner={owner!r} 往返失败"
            assert decoded == (12345, owner)

    def test_handle_is_opaque_no_raw_pid_or_owner(self) -> None:
        """handle 不含裸 pid/owner 明文（不透明引用契约）。"""
        handle = encode_handle(98765, "sess-owner-1")
        assert "98765" not in handle
        assert "sess-owner-1" not in handle

    def test_tampered_body_rejected(self) -> None:
        """篡改 body（换 pid/owner/校验和任一位）→ 解码拒绝，不猜。"""
        handle = encode_handle(111, "sess-a")
        scheme, body, digest = handle.split(".")
        # 校验和与 body 不匹配（body 篡改）
        assert decode_handle(f"{scheme}.{body[:-4]}aaaa.{digest}") is None
        # body 合法但校验和错
        assert decode_handle(f"{scheme}.{body}.deadbeef") is None
        # 未知 scheme
        assert decode_handle(f"xx.{body}.{digest}") is None
        # 缺段 / 乱串
        assert decode_handle("not-a-handle") is None
        assert decode_handle("") is None

    def test_decode_non_string_returns_none(self) -> None:
        """非字符串输入不抛异常（None/整数安全拒绝）。"""
        assert decode_handle(None) is None  # type: ignore[arg-type]
        assert decode_handle(12345) is None  # type: ignore[arg-type]

    def test_handle_carries_owner_for_pid_reuse_guard(self) -> None:
        """同 pid 不同 owner 编出不同 handle（pid 复用防劫持的编码基础）。"""
        h_a = encode_handle(42, "sess-a")
        h_b = encode_handle(42, "sess-b")
        assert h_a != h_b
        assert decode_handle(h_a) == (42, "sess-a")

    def test_payload_pid_not_int_rejected(self) -> None:
        """校验和合法但 payload pid 非整数（int() 失败）/缺 pid/payload 非字典 → 拒绝。"""
        assert decode_handle(_craft_handle({"pid": "abc", "owner": "s"})) is None
        assert decode_handle(_craft_handle({"owner": "s"})) is None
        assert decode_handle(_craft_handle([1, 2])) is None

    def test_payload_nonpositive_pid_rejected(self) -> None:
        """pid<=0（伪造/复用残留）→ 拒绝。"""
        assert decode_handle(_craft_handle({"pid": 0, "owner": "s"})) is None
        assert decode_handle(_craft_handle({"pid": -3, "owner": "s"})) is None

    def test_payload_non_string_owner_rejected(self) -> None:
        """owner 非字符串（类型不符）→ 拒绝，不猜。"""
        assert decode_handle(_craft_handle({"pid": 7, "owner": 123})) is None
        assert decode_handle(_craft_handle({"pid": 7, "owner": ["s"]})) is None


# ══════════════════════════════════════════════════════════════════
# 判据 a：bash 前台完成返回完整输出
# ══════════════════════════════════════════════════════════════════


@_NEEDS_SHELL
class TestBashForegroundComplete:
    @pytest.mark.asyncio
    async def test_foreground_completed_returns_full_output(self, tmp_path) -> None:
        """前台快命令：completed + handle + 完整输出 + exit_code=0，无 pid 字段。"""
        tool = _make_tool(tmp_path)
        result = await tool.run_command(
            {"command": "echo foreground_alive", "timeout": 15, "workspace": str(tmp_path)}
        )
        assert result.success, result.error
        data = result.output
        assert data["status"] == "completed"
        assert "foreground_alive" in data["output"]
        assert data["exit_code"] == 0
        assert isinstance(data["handle"], str)
        assert decode_handle(data["handle"]) is not None
        assert "pid" not in data, "工具面 schema 用 handle，pid 不再外露"

    @pytest.mark.asyncio
    async def test_foreground_nonzero_exit_reports_failure_with_handle(self, tmp_path) -> None:
        """前台非零退出：COMMAND_FAILED 失败结果（失败信封携带稳定 error_code）。"""
        tool = _make_tool(tmp_path)
        result = await tool.run_command(
            {"command": "false", "timeout": 15, "workspace": str(tmp_path)}
        )
        assert not result.success
        assert result.error_code == "COMMAND_FAILED"

    @pytest.mark.asyncio
    async def test_missing_command_rejected(self, tmp_path) -> None:
        """command 缺失 → MISSING_COMMAND。"""
        tool = _make_tool(tmp_path)
        result = await tool.run_command({"workspace": str(tmp_path)})
        assert not result.success
        assert result.error_code == "MISSING_COMMAND"


# ══════════════════════════════════════════════════════════════════
# 判据 b：前台超时 = running+handle+已捕获输出，进程不死
# ══════════════════════════════════════════════════════════════════


@_NEEDS_SHELL
class TestForegroundTimeoutRunning:
    @pytest.mark.asyncio
    async def test_timeout_returns_running_with_captured_output(self, tmp_path) -> None:
        """超时：running + handle + 全部已捕获输出；进程仍在跑（log 后续等到完成）。"""
        tool = _make_tool(tmp_path)
        result = await tool.run_command(
            {
                "command": _py_cmd(
                    "print('captured-before-timeout', flush=True); "
                    "import time; time.sleep(8)"
                ),
                "timeout": 2,
                "workspace": str(tmp_path),
            }
        )
        assert result.success, result.error
        data = result.output
        assert data["status"] == "running"
        handle = data["handle"]
        decoded = decode_handle(handle)
        assert decoded is not None
        pid = decoded[0]
        assert "captured-before-timeout" in data["output"], "超时返回必须附带已捕获输出"

        # 进程不死：仍在 active_processes
        info = tool.process_manager.get_process_info(pid)
        assert info is not None, "超时不得杀进程（进程已消失）"
        assert info.status == "running", "超时不得杀进程"

        # log(block=true) 后续等到完成
        follow = await tool.read_log({"handle": handle, "block": True, "workspace": str(tmp_path)})
        assert follow.success, follow.error
        assert follow.output["status"] == "completed"
        assert follow.output["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_timeout_huge_value_clamped_to_max(self, tmp_path) -> None:
        """timeout=99999 钳到 MAX_TIMEOUT 上限（小上限注入验证钳制公约）。"""

        class _ShortCapTool(BashTool):
            MAX_TIMEOUT = 1

        tool = _ShortCapTool()
        tool.process_manager = ProcessManager(log_dir=tmp_path / "logs")
        started = time.monotonic()
        result = await tool.run_command(
            {"command": _py_cmd("import time; time.sleep(10)"), "timeout": 99999,
             "workspace": str(tmp_path)}
        )
        wall = time.monotonic() - started
        try:
            assert result.success, result.error
            assert result.output["status"] == "running"
            assert wall < 30, "钳制后必须有限时间返回"
            assert wall >= 0.9, "返回前应等待满一个钳后上限周期"
        finally:
            decoded = decode_handle(result.output["handle"])
            if decoded:
                await tool.process_manager.terminate_process(decoded[0], force=True)

    def test_max_timeout_is_600(self) -> None:
        """ADR 决策 3：前台超时上限 290 → 600。"""
        assert BashTool.MAX_TIMEOUT == 600


# ══════════════════════════════════════════════════════════════════
# 判据 c：background=true 立即返回 handle
# ══════════════════════════════════════════════════════════════════


@_NEEDS_SHELL
class TestBackgroundImmediate:
    @pytest.mark.asyncio
    async def test_background_returns_immediately_with_handle(self, tmp_path) -> None:
        """background=true：立即返回 running+handle（不等命令完成）。"""
        tool = _make_tool(tmp_path)
        started = time.monotonic()
        result = await tool.run_command(
            {
                "command": _py_cmd("import time; time.sleep(8); print('late')"),
                "background": True,
                "workspace": str(tmp_path),
            }
        )
        wall = time.monotonic() - started
        assert result.success, result.error
        assert wall < 10, "background 必须立即返回（sleep 8s 远小于等待上限）"
        assert result.output["status"] == "running"
        handle = result.output["handle"]
        decoded = decode_handle(handle)
        assert decoded is not None
        pid, _owner = decoded
        try:
            info = tool.process_manager.get_process_info(pid)
            assert info is not None
            assert info.status == "running"
        finally:
            await tool.process_manager.terminate_process(pid, force=True)


# ══════════════════════════════════════════════════════════════════
# 判据 d：log block 两态
# ══════════════════════════════════════════════════════════════════


@_NEEDS_SHELL
class TestLogBothModes:
    @pytest.mark.asyncio
    async def test_block_false_reads_running_state_from_memory(self, tmp_path) -> None:
        """block=false：进程运行中立即返回 running + 当前输出（不等待）。"""
        tool = _make_tool(tmp_path)
        started = await tool.run_command(
            {
                "command": _py_cmd("print('early-line', flush=True); import time; time.sleep(8)"),
                "background": True,
                "workspace": str(tmp_path),
            }
        )
        handle = started.output["handle"]
        decoded = decode_handle(handle)
        assert decoded is not None
        pid = decoded[0]
        try:
            await _await_output(tool, pid, "early-line")
            read = await tool.read_log({"handle": handle, "block": False, "workspace": str(tmp_path)})
            assert read.success, read.error
            assert read.output["status"] == "running"
            assert "early-line" in read.output["output"]
        finally:
            await tool.process_manager.terminate_process(pid, force=True)

    @pytest.mark.asyncio
    async def test_block_false_disk_path_after_cleanup(self, tmp_path) -> None:
        """block=false：进程已清（即时清理）→ 磁盘日志路径，completed + 完整输出。"""
        tool = _make_tool(tmp_path)
        result = await tool.run_command(
            {"command": "echo persisted_for_log", "timeout": 15, "workspace": str(tmp_path)}
        )
        assert result.success
        handle = result.output["handle"]
        pid = decode_handle(handle)[0]
        assert pid not in tool.process_manager.active_processes, "快命令完成后应已被即时清理"

        read = await tool.read_log({"handle": handle, "block": False, "workspace": str(tmp_path)})
        assert read.success, read.error
        assert read.output["status"] == "completed"
        assert "persisted_for_log" in read.output["output"]

    @pytest.mark.asyncio
    async def test_block_true_waits_until_completion(self, tmp_path) -> None:
        """block=true：等到完成（等待上限内），返回 completed + 真实退出码。"""
        tool = _make_tool(tmp_path)
        started = await tool.run_command(
            {
                "command": _py_cmd("import time; time.sleep(3); print('waited-done')"),
                "background": True,
                "workspace": str(tmp_path),
            }
        )
        handle = started.output["handle"]
        follow = await tool.read_log({"handle": handle, "block": True, "workspace": str(tmp_path)})
        assert follow.success, follow.error
        assert follow.output["status"] == "completed"
        assert follow.output["exit_code"] == 0
        assert "waited-done" in follow.output["output"]

    @pytest.mark.asyncio
    async def test_block_true_cap_returns_running(self, tmp_path) -> None:
        """block=true 等待上限到点：running+handle 返回（不杀进程）。"""

        class _ShortCapTool(BashTool):
            MAX_TIMEOUT = 1

        tool = _ShortCapTool()
        tool.process_manager = ProcessManager(log_dir=tmp_path / "logs")
        started = await tool.run_command(
            {"command": _py_cmd("import time; time.sleep(10)"), "background": True,
             "workspace": str(tmp_path)}
        )
        handle = started.output["handle"]
        pid = decode_handle(handle)[0]
        try:
            read = await tool.read_log({"handle": handle, "block": True, "workspace": str(tmp_path)})
            assert read.success, read.error
            assert read.output["status"] == "running"
            assert read.output["handle"] == handle
        finally:
            await tool.process_manager.terminate_process(pid, force=True)

    @pytest.mark.asyncio
    async def test_unknown_handle_fails_explicitly(self, tmp_path) -> None:
        """无对应日志的合法 handle（双无身份）：block=false → LOG_FILE_NOT_FOUND。"""
        tool = _make_tool(tmp_path)
        handle = encode_handle(999999, None)
        # 不注入 workspace/session_id——caller 与 handle 双无身份走放行兼容路径，
        # 让「日志不存在」的 NOT_FOUND 语义显式呈现
        read = await tool.read_log({"handle": handle, "block": False})
        assert not read.success
        assert read.error_code == "LOG_FILE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_missing_handle_rejected(self, tmp_path) -> None:
        """handle 缺失 → MISSING_HANDLE；畸形 handle → INVALID_HANDLE。"""
        tool = _make_tool(tmp_path)
        missing = await tool.read_log({"block": False})
        assert not missing.success
        assert missing.error_code == "MISSING_HANDLE"

        invalid = await tool.read_log({"handle": "../../windows", "block": False})
        assert not invalid.success
        assert invalid.error_code == "INVALID_HANDLE"

    @pytest.mark.asyncio
    async def test_block_false_memory_completed_includes_exit_code(self, tmp_path) -> None:
        """block=false：进程仍在内存但已完成 → 终态透出 exit_code（内存路径）。"""
        tool = _make_tool(tmp_path)
        log_file = tool.process_manager.log_dir / "bash_8101.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("done-out\n", encoding="utf-8")
        tool.process_manager.active_processes[8101] = ProcessInfo(
            pid=8101,
            command="echo done-out",
            start_time=time.time(),
            log_file=log_file,
            status="completed",
            exit_code=3,
            metadata={"owner": "sess-x"},
        )
        handle = encode_handle(8101, "sess-x")
        read = await tool.read_log({"handle": handle, "block": False, "session_id": "sess-x"})
        assert read.success, read.error
        assert read.output["status"] == "completed"
        assert read.output["exit_code"] == 3, "内存终态必须透出 exit_code"


async def _await_output(tool: BashTool, pid: int, fragment: str, timeout: float = 10.0) -> None:
    """轮询直到输出含 fragment（背景进程输出就绪的确定性等待）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fragment in tool.process_manager.get_output(pid):
            return
        import asyncio

        await asyncio.sleep(0.2)
    raise AssertionError(f"输出 {timeout}s 内未出现: {fragment!r}")


# ══════════════════════════════════════════════════════════════════
# 判据 e：input 生效
# ══════════════════════════════════════════════════════════════════


@_NEEDS_SHELL
class TestInputSendsText:
    @pytest.mark.asyncio
    async def test_input_reaches_waiting_process(self, tmp_path) -> None:
        """read 等输入 → input(handle, text) → 进程收到并回显。"""
        tool = _make_tool(tmp_path)
        started = await tool.run_command(
            {
                "command": 'bash -c \'read answer; echo "got: $answer"\'',
                "background": True,
                "workspace": str(tmp_path),
            }
        )
        handle = started.output["handle"]
        pid = decode_handle(handle)[0]
        ws = str(tmp_path)
        try:
            import asyncio

            await asyncio.sleep(1.0)  # 等 read 进入等待输入态
            # owner 身份派生自 workspace，spawn/input 必须同身份才能过 handle 校验
            sent = await tool.send_input({"handle": handle, "text": "hello", "workspace": ws})
            assert sent.success, sent.error
            assert sent.output["handle"] == handle

            info = await _wait_done(tool, pid)
            assert info["exit_code"] == 0
            assert "hello" in tool.process_manager.get_output(pid)
        finally:
            await tool.process_manager.terminate_process(pid, force=True)

    @pytest.mark.asyncio
    async def test_input_missing_text_rejected(self, tmp_path) -> None:
        """text 缺失 → MISSING_TEXT。"""
        tool = _make_tool(tmp_path)
        result = await tool.send_input({"handle": encode_handle(1, None)})
        assert not result.success
        assert result.error_code == "MISSING_TEXT"


async def _wait_done(tool: BashTool, pid: int, timeout: float = 20.0) -> dict:
    """轮询等进程结束，返回 {exit_code}（即时清理时降级磁盘真理源）。"""
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = tool.process_manager.get_process_info(pid)
        if info is None or info.status != "running":
            exit_code = None
            if info is not None:
                exit_code = info.exit_code
            else:
                file_data = tool.process_manager.read_log_by_pid(pid)
                exit_code = file_data.get("exit_code") if file_data else None
            assert exit_code is not None, "进程结束后退出码必须可查"
            return {"exit_code": exit_code}
        await asyncio.sleep(0.2)
    raise AssertionError("进程未在超时内结束")


# ══════════════════════════════════════════════════════════════════
# 判据 f：kill 后 log 仍可读完整输出（T1 flush 契约贯通）
# ══════════════════════════════════════════════════════════════════


@_NEEDS_SHELL
class TestKillPreservesLog:
    @pytest.mark.asyncio
    async def test_kill_then_log_reads_full_output_with_agent_reason(self, tmp_path) -> None:
        """kill：terminated 结果 + 日志完整（击杀前输出 + agent 终止行）。"""
        tool = _make_tool(tmp_path)
        started = await tool.run_command(
            {
                "command": _py_cmd(
                    "print('pre-kill-line', flush=True); import time; time.sleep(30)"
                ),
                "background": True,
                "workspace": str(tmp_path),
            }
        )
        handle = started.output["handle"]
        pid = decode_handle(handle)[0]
        try:
            await _await_output(tool, pid, "pre-kill-line")
            killed = await tool.terminate({"handle": handle, "workspace": str(tmp_path)})
            assert killed.success, killed.error
            assert killed.output["status"] == "terminated"
            assert killed.output["handle"] == handle

            # kill 后 log 仍可读完整输出（磁盘真理源：输出在前、终止行收尾）
            read = await tool.read_log({"handle": handle, "block": False, "workspace": str(tmp_path)})
            assert read.success, read.error
            assert "pre-kill-line" in read.output["output"], "击杀前输出必须完整可取"
            assert read.output.get("terminated_by") == "agent", "agent 发起即 agent 语义"
        finally:
            try:
                await tool.process_manager.terminate_process(pid, force=True)
            except Exception:  # noqa: BLE001 — 已终止时兜底清理失败不影响判定
                pass

    @pytest.mark.asyncio
    async def test_kill_unknown_handle_fails(self, tmp_path) -> None:
        """kill 无主 handle → PROCESS_NOT_FOUND。"""
        tool = _make_tool(tmp_path)
        result = await tool.terminate({"handle": encode_handle(424242, None)})
        assert not result.success
        assert result.error_code == "PROCESS_NOT_FOUND"


# ══════════════════════════════════════════════════════════════════
# 判据 g：越权 handle 拒绝
# ══════════════════════════════════════════════════════════════════


class TestHandleOwnerGuard:
    @pytest.mark.asyncio
    async def test_owner_mismatch_rejected_log(self, tmp_path) -> None:
        tool = _make_tool(tmp_path)
        handle = encode_handle(6002, "sess-a")
        result = await tool.read_log({"handle": handle, "session_id": "sess-b"})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"

    @pytest.mark.asyncio
    async def test_owner_mismatch_rejected_input(self, tmp_path) -> None:
        tool = _make_tool(tmp_path)
        handle = encode_handle(6003, "sess-a")
        result = await tool.send_input({"handle": handle, "text": "hi", "session_id": "sess-b"})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"

    @pytest.mark.asyncio
    async def test_owner_mismatch_rejected_kill(self, tmp_path) -> None:
        tool = _make_tool(tmp_path)
        handle = encode_handle(6004, "sess-a")
        result = await tool.terminate({"handle": handle, "session_id": "sess-b"})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"

    @pytest.mark.asyncio
    async def test_unidentified_caller_on_owned_handle_rejected(self, tmp_path) -> None:
        """无身份调用方操作有主 handle → 拒绝（严格语义保留）。"""
        tool = _make_tool(tmp_path)
        handle = encode_handle(6005, "sess-a")
        result = await tool.read_log({"handle": handle})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"

    @pytest.mark.asyncio
    async def test_owner_match_allowed(self, tmp_path) -> None:
        """同会话 handle：放行（对照组）。"""
        tool = _make_tool(tmp_path)
        handle = encode_handle(6006, "sess-a")
        result = await tool.read_log({"handle": handle, "session_id": "sess-a"})
        # 6006 无真实进程/日志——放行后落 LOG_FILE_NOT_FOUND（而非 FORBIDDEN）
        assert not result.success
        assert result.error_code == "LOG_FILE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_live_process_owner_mismatch_rejected(self, tmp_path) -> None:
        """pid 复用防劫持：handle owner 与活进程 metadata owner 不符 → 拒绝。"""
        tool = _make_tool(tmp_path)
        _inject_live_process(tool, 7001, "sess-live")
        # handle 伪造为 sess-a（合法校验和但归属不符），调用方自称 sess-a 过第一段
        handle = encode_handle(7001, "sess-a")
        result = await tool.read_log({"handle": handle, "session_id": "sess-a"})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"

    @pytest.mark.asyncio
    async def test_live_owner_mismatch_rejected_log_block_true(self, tmp_path) -> None:
        """block=true 路径的第二段身份校验同样拒绝归属不符的活进程。"""
        tool = _make_tool(tmp_path)
        _inject_live_process(tool, 7101, "sess-live")
        handle = encode_handle(7101, "sess-a")
        result = await tool.read_log({"handle": handle, "block": True, "session_id": "sess-a"})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"

    @pytest.mark.asyncio
    async def test_live_owner_mismatch_rejected_input(self, tmp_path) -> None:
        """input 路径的第二段身份校验拒绝归属不符的活进程（发文本前拦截）。"""
        tool = _make_tool(tmp_path)
        _inject_live_process(tool, 7102, "sess-live")
        handle = encode_handle(7102, "sess-a")
        result = await tool.send_input({"handle": handle, "text": "hi", "session_id": "sess-a"})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"

    @pytest.mark.asyncio
    async def test_live_owner_mismatch_rejected_kill(self, tmp_path) -> None:
        """kill 路径的第二段身份校验拒绝归属不符的活进程（终止前拦截）。"""
        tool = _make_tool(tmp_path)
        _inject_live_process(tool, 7103, "sess-live")
        handle = encode_handle(7103, "sess-a")
        result = await tool.terminate({"handle": handle, "session_id": "sess-a"})
        assert not result.success
        assert result.error_code == "PROCESS_FORBIDDEN"
