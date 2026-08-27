"""容器路径后台执行测试 — TDD 红灯。

覆盖目标：`use_docker=True` 的 bash_execute 也能像本地路径一样
"execute 超时返回 status=running+pid → continue 轮询 → terminate 容器内杀"。

实现路径：`ProcessManager.start_process` 接受 `container_id` 参数，
有 container_id 时用 `docker exec` 起进程；命令包装成 `echo $$; exec <cmd>`，
从 stdout 第一行读出**容器内 pid**（不是宿主机 docker exec 客户端的 pid），
存进 ProcessInfo.metadata。后续 terminate 走 ContainerProcessBackend，
用容器内 pid 调 `docker exec <cid> kill`。

关键正确性：宿主机 process.pid ≠ 容器内 pid。容器内 sh 自己报 $$ 才是真的目标 pid。

本文件测试不依赖真实 docker —— 全部 mock `asyncio.create_subprocess_exec`
（验证 docker exec 参数）和 `backend._run_cmd`（验证 kill 命令）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bash_types import WorkUnit
from process_manager import (
    ContainerProcessBackend,
    LocalProcessBackend,
    ProcessManager,
    _get_container_backend,
)
from tool import BashTool

pytestmark = pytest.mark.unit


# ============================================================
# Helpers
# ============================================================


class _FakeStream:
    """可往里塞字节的假 StreamReader，给 _read_output 用。

    keep_open=True 时读完缓冲后不返回 EOF（模拟进程还活着），
    read/readline 永久阻塞——用于测试 terminate/轮询时进程未退出的场景。

    同时实现 read(n) 和 readline()，因为 _read_output 现在用 read(4096)
    按块读（块缓冲根治），不再用 readline。
    """

    def __init__(self, *lines: bytes, keep_open: bool = False) -> None:
        self._buf = bytearray()
        for ln in lines:
            self._buf.extend(ln)
            if not ln.endswith(b"\n"):
                self._buf.extend(b"\n")
        self._keep_open = keep_open
        self._eof = False

    async def read(self, n: int = -1) -> bytes:
        """按块读，模拟 _read_output 的 stream.read(4096) 调用。"""
        if self._buf:
            # 返回缓冲中可用字节（最多 n 字节）
            take = min(n, len(self._buf)) if n > 0 else len(self._buf)
            chunk = bytes(self._buf[:take])
            del self._buf[:take]
            return chunk
        # 缓冲空
        if self._keep_open:
            # 模拟进程还活着：永久阻塞（让出控制权，不返回 EOF）
            await asyncio.sleep(3600)
            return b""
        if not self._eof:
            self._eof = True
            await asyncio.sleep(0)
        return b""

    async def readline(self) -> bytes:
        if self._buf:
            idx = self._buf.find(b"\n")
            if idx < 0:
                line = bytes(self._buf)
                self._buf.clear()
                return line
            line = bytes(self._buf[: idx + 1])
            del self._buf[: idx + 1]
            return line
        # 缓冲空
        if self._keep_open:
            # 模拟进程还活着：永久阻塞（让出控制权，不返回 EOF）
            await asyncio.sleep(3600)
            return b""
        if not self._eof:
            self._eof = True
            await asyncio.sleep(0)
        return b""


def _make_fake_process(
    host_pid: int,
    container_pid: int | None = None,
    keep_open: bool = False,
) -> MagicMock:
    """构造假 asyncio.subprocess.Process。

    container_pid 给定时，stdout 第一行返回该 pid（模拟 `echo $$`）。
    keep_open=True 时进程"还活着"（stdout 不 EOF、wait 阻塞），
    用于测试 terminate/轮询时进程未退出的场景。
    """
    proc = MagicMock()
    proc.pid = host_pid
    # returncode 必须显式设 None，否则 MagicMock 默认返回 truthy MagicMock，
    # _sync_poll_process 会误判进程已退出（status→error/completed）。
    proc.returncode = None
    if container_pid is not None:
        first_line = f"{container_pid}\n".encode()
        proc.stdout = _FakeStream(first_line, keep_open=keep_open)
        proc.stderr = _FakeStream(keep_open=keep_open)
    else:
        proc.stdout = _FakeStream(keep_open=keep_open)
        proc.stderr = _FakeStream(keep_open=keep_open)
    proc.stdin = MagicMock()

    if keep_open:
        # 进程"还活着"：stream 阻塞（status 保持 running），但 wait 立即返回 0
        # （模拟 kill 后进程退出，避免 terminate 的 wait_for 等 5s 超时）。
        proc.wait = AsyncMock(return_value=0)
    else:
        proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    return proc


# ============================================================
# Step 1: start_process 容器分支（路由核心）
# ============================================================


@pytest.mark.asyncio
async def test_start_process_with_container_id_uses_docker_exec(tmp_path):
    """传 container_id → create_subprocess_exec 的参数应是 docker exec ...，不是 bash -c。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=42, container_pid=100)

    captured_args: list = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured_args.extend(args)
        return fake_proc

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        side_effect=fake_create_subprocess_exec,
    ):
        pid, _ = await pm.start_process(
            command="echo hello",
            working_dir="/workspace",
            container_id="abc123",
        )

    # 必须是 docker exec -w <workdir> <container> sh -c <cmd>
    assert captured_args[0] == "docker"
    assert "exec" in captured_args
    assert "-w" in captured_args
    assert "/workspace" in captured_args
    assert "abc123" in captured_args
    assert "sh" in captured_args
    assert "-c" in captured_args
    # 命令必须被包装成 echo $$; exec <cmd>（否则拿不到容器内 pid）
    cmd_idx = captured_args.index("-c") + 1
    wrapped = captured_args[cmd_idx]
    assert "echo $$" in wrapped, f"命令应被包装成 echo $$; exec <cmd>，实际: {wrapped}"
    assert "echo hello" in wrapped
    # 不应该出现 wsl / cmd 这种本地 shell
    assert captured_args[0] != "wsl"
    assert "cmd" not in captured_args


@pytest.mark.asyncio
async def test_start_process_without_container_id_unchanged(tmp_path):
    """不传 container_id → 走原 WSL/Bash/CMD 分支（回归保护）。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=7)

    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return fake_proc

    async def fake_shell(*args, **kwargs):
        captured_args.extend(args)
        return fake_proc

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ), patch(
        "process_manager.asyncio.create_subprocess_shell",
        side_effect=fake_shell,
    ):
        await pm.start_process(command="echo hi", working_dir=str(tmp_path))

    assert captured_args, "应当调用某种本地 subprocess 创建函数"
    assert captured_args[0] != "docker", "无 container_id 时不应走 docker exec"
    assert "docker" not in captured_args


@pytest.mark.asyncio
async def test_start_process_container_stores_container_pid_in_metadata(tmp_path):
    """传 container_id → ProcessInfo.metadata['container_pid'] 应是容器内 pid（100），不是 host pid（42）。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    # host_pid=42 是 docker exec 客户端；container_pid=100 才是容器内 sh
    fake_proc = _make_fake_process(host_pid=42, container_pid=100)

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        pid, _ = await pm.start_process(
            command="sleep 1",
            container_id="cid",
        )

    # 返回的 pid 仍是宿主机视角的 key（用于 active_processes 索引）
    assert pid == 42
    info = pm.get_process_info(42)
    assert info is not None
    assert info.metadata.get("container_pid") == 100, (
        "容器内 pid 必须存在 metadata，否则 kill 时杀错对象"
    )
    assert info.metadata.get("container_id") == "cid"


@pytest.mark.asyncio
async def test_start_process_container_returns_host_pid_as_key(tmp_path):
    """active_processes 的 key 用宿主机 pid（asyncio 句柄的 pid），方便 _read_output/wait。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=999, container_pid=1)

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        pid, _ = await pm.start_process(command="x", container_id="cid")

    assert pid == 999
    assert 999 in pm.active_processes


@pytest.mark.asyncio
async def test_start_process_container_ignores_host_working_dir(tmp_path):
    """容器场景 working_dir 必须用容器内路径（/workspace），忽略传入的宿主路径。

    根因：BashTool.get_working_dir 返回 task workspace（宿主 Windows 路径如
    D:\\myproject\\xxx），直接传给 `docker exec -w <宿主路径>` 会让 OCI 报
    'Cwd must be an absolute path' 退 128。容器挂载点固定是 /workspace
    （IsolationManager 约定），容器场景强制用它。
    """
    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=42, container_pid=100)

    captured_args: list = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured_args.extend(args)
        return fake_proc

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        side_effect=fake_create_subprocess_exec,
    ):
        # 传入宿主 Windows 路径（模拟 BashTool.get_working_dir 的返回值）
        await pm.start_process(
            command="echo hi",
            working_dir=r"D:\myproject\task_xyz",
            container_id="cid",
        )

    # docker exec -w 后面必须是容器内路径 /workspace，不能是宿主路径
    w_idx = captured_args.index("-w")
    actual_workdir = captured_args[w_idx + 1]
    assert actual_workdir == "/workspace", (
        f"容器场景 working_dir 必须强制 /workspace，实际传了: {actual_workdir!r}"
    )
    # 不应出现宿主路径片段
    assert "myproject" not in str(captured_args), (
        f"宿主路径不应透传到容器，实际 args: {captured_args}"
    )


# ============================================================
# Step 2: ContainerProcessBackend（杀进程的容器实现）
# ============================================================


@pytest.mark.asyncio
async def test_container_backend_kill_uses_docker_exec_kill_single_pid(tmp_path):
    """kill → 发 `docker exec <cid> kill -9 <container_pid>`，单进程杀，不整组（-PGID）。"""
    backend = ContainerProcessBackend(container_id="abc")

    captured: list = []

    async def fake_run_cmd(args, timeout=30):
        captured.append(args)
        return 0, b"", b""

    backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]
    await backend.kill(
        WorkUnit(pid=100, command="x", metadata={"container_id": "abc"})
    )

    assert len(captured) == 1
    args = captured[0]
    # docker exec <cid> sh -c 'kill -9 <container_pid> ...'
    assert args[:3] == ["docker", "exec", "abc"]
    assert "sh" in args and "-c" in args
    # 合并所有 args 检查 kill 命令内容（kill 现在在 sh -c 字符串里）
    cmd_str = " ".join(args)
    assert "kill -9" in cmd_str, f"应有 'kill -9'，实际: {cmd_str}"
    assert "100" in cmd_str, "应用容器内 pid（100），不是 host pid"
    # 不应出现整组杀 -- -PGID
    assert "--" not in cmd_str, "不应整组杀（runc 卡死坑）"
    assert "-PGID" not in cmd_str and "-pgid" not in cmd_str


@pytest.mark.asyncio
async def test_container_backend_kill_force_false_uses_sigterm():
    """kill(force=False) → docker exec kill 15（SIGTERM），不是 -9。"""
    backend = ContainerProcessBackend(container_id="abc")

    captured: list = []

    async def fake_run_cmd(args, timeout=30):
        captured.append(args)
        return 0, b"", b""

    backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]
    await backend.kill(
        WorkUnit(pid=42, command="x", metadata={"container_id": "abc"}),
        force=False,
    )

    args = captured[0]
    cmd_str = " ".join(args)
    assert "kill -15" in cmd_str, f"force=False 应发 SIGTERM (15)，实际: {cmd_str}"
    assert "-9" not in cmd_str


@pytest.mark.asyncio
async def test_container_backend_kill_handles_already_dead():
    """docker exec kill 返回非零（进程已退出）→ 不抛异常，kill 命令仍完整发出。"""
    backend = ContainerProcessBackend(container_id="abc")

    captured: list = []

    async def fake_run_cmd(args, timeout=30):
        captured.append(args)
        return 1, b"", b"no such process"

    backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]
    # 非零退出码被容忍：默认 force=True 发 SIGKILL，连杀两次均不抛（幂等）
    for _ in range(2):
        await backend.kill(
            WorkUnit(pid=999, command="x", metadata={"container_id": "abc"})
        )

    assert len(captured) == 2
    cmd_str = " ".join(captured[0])
    assert "sh" in cmd_str, f"应经 sh -c 走内建 kill: {cmd_str}"
    assert "-c" in cmd_str
    assert "kill -9 999" in cmd_str, f"force 默认 True 应发 SIGKILL: {cmd_str}"


@pytest.mark.asyncio
async def test_container_backend_kill_missing_container_id_raises():
    """WorkUnit.metadata 缺 container_id 且后端实例也无 → 明确报错，不静默吞。

    注：container_id 来源优先 unit.metadata，回退 backend.container_id。
    本测试构造一个 container_id="" 的后端 + 空 metadata，验证两者都缺时报错。
    """
    backend = ContainerProcessBackend(container_id="")
    unit = WorkUnit(pid=42, command="x", metadata={})
    with pytest.raises(KeyError):
        await backend.kill(unit)


@pytest.mark.asyncio
async def test_container_backend_sample_memory_returns_none():
    """容器场景不做宿主内存采样（靠 docker -m OOM 兜底）。"""
    backend = ContainerProcessBackend(container_id="abc")
    assert await backend.sample_memory() is None


@pytest.mark.asyncio
async def test_container_backend_sample_unit_memory_returns_none():
    """容器场景不做单进程内存采样（看门狗跳过内存判据）。"""
    backend = ContainerProcessBackend(container_id="abc")
    result = await backend.sample_unit_memory(
        WorkUnit(pid=42, command="x", metadata={"container_id": "abc"})
    )
    assert result is None


# ============================================================
# Step 3: ProcessInfo.backend 注入容器后端
# ============================================================


@pytest.mark.asyncio
async def test_start_process_container_assigns_container_backend(tmp_path):
    """传 container_id → ProcessInfo.backend 应是 ContainerProcessBackend，不是 Local。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=42, container_pid=100)

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        await pm.start_process(command="x", container_id="cid")

    info = pm.get_process_info(42)
    assert info is not None
    assert isinstance(info.backend, ContainerProcessBackend)


@pytest.mark.asyncio
async def test_start_process_local_assigns_local_backend(tmp_path):
    """不传 container_id → ProcessInfo.backend 应是 LocalProcessBackend（回归）。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=8)

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ), patch(
        "process_manager.asyncio.create_subprocess_shell",
        return_value=fake_proc,
    ):
        await pm.start_process(command="x")

    info = pm.get_process_info(8)
    assert info is not None
    assert isinstance(info.backend, LocalProcessBackend)


@pytest.mark.asyncio
async def test_terminate_container_process_calls_docker_kill(tmp_path):
    """terminate_process 在容器进程上 → 走 docker exec kill（容器内 pid），不是 psutil 整树杀。"""
    pm = ProcessManager(log_dir=tmp_path / "logs")
    # host_pid=42 是 docker exec 客户端，container_pid=100 是容器内 sh
    # keep_open=True 模拟进程还活着（terminate 一个 running 进程）
    fake_proc = _make_fake_process(host_pid=42, container_pid=100, keep_open=True)

    kill_called: list = []

    async def fake_run_cmd(args, timeout=30):
        kill_called.append(args)
        return 0, b"", b""

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        await pm.start_process(command="x", container_id="cid")

    info = pm.get_process_info(42)
    assert info is not None
    assert isinstance(info.backend, ContainerProcessBackend)
    info.backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]

    await pm.terminate_process(42, force=True)

    assert kill_called, "应通过 docker exec kill 杀容器进程"
    args = kill_called[0]
    assert args[:3] == ["docker", "exec", "cid"]
    cmd_str = " ".join(args)
    assert "kill -9" in cmd_str, f"应有 kill -9，实际: {cmd_str}"
    assert "100" in cmd_str, "应杀容器内 pid (100)，不是 host pid (42)"


@pytest.mark.asyncio
async def test_get_container_backend_caches_by_container_id():
    """_get_container_backend 按 container_id 缓存（同 cid 返回同一实例）。"""
    b1 = _get_container_backend("cid_a")
    b2 = _get_container_backend("cid_a")
    b3 = _get_container_backend("cid_b")
    assert b1 is b2, "同 container_id 应返回同一实例"
    assert b1 is not b3, "不同 container_id 应返回不同实例"


# ============================================================
# Step 5: BashTool 透传 container_id
# ============================================================


@pytest.mark.asyncio
async def test_bashtool_execute_passes_container_id_to_start_process(tmp_path):
    """BashTool.execute 收到 _container_id → start_process 被调用时带 container_id。

    0.2 契约（tool.py _handle_execute）：inputs._container_id 透传到
    ProcessManager.start_process 的 container_id 形参。
    """
    tool = BashTool()
    captured: dict = {}

    async def fake_start_process(command, working_dir=None, env=None, log_dir=None, container_id=None, provider_kind=None, bwrap_pid=None, owner=None, on_output=None):
        captured["command"] = command
        captured["container_id"] = container_id
        # 返回一个立刻完成的假 pid
        return 1, tmp_path / "fake.log"

    tool.process_manager.start_process = fake_start_process  # type: ignore[assignment]
    # 让 get_process_info 返回 completed，避免轮询
    from bash_types import ProcessInfo
    fake_info = ProcessInfo(
        pid=1, command="x", start_time=0, log_file=tmp_path / "fake.log",
        status="completed", exit_code=0,
    )
    tool.process_manager.get_process_info = lambda pid: fake_info  # type: ignore[assignment]
    tool.process_manager.get_summary = lambda pid: {"exit_code": 0, "summary": []}  # type: ignore[assignment]
    tool.process_manager.get_output = lambda pid: "done"  # type: ignore[assignment]

    result = await tool.execute({
        "command": "echo hi",
        "_container_id": "cid_xyz",
    })

    assert captured.get("container_id") == "cid_xyz", (
        f"start_process 应收到 container_id='cid_xyz'，实际: {captured.get('container_id')}"
    )


@pytest.mark.asyncio
async def test_bashtool_execute_without_container_id_passes_none(tmp_path):
    """BashTool.execute 无 _container_id → start_process 的 container_id 应为 None（回归）。"""
    tool = BashTool()
    captured: dict = {}

    async def fake_start_process(command, working_dir=None, env=None, log_dir=None, container_id=None, provider_kind=None, bwrap_pid=None):
        captured["container_id"] = container_id
        return 1, tmp_path / "fake.log"

    tool.process_manager.start_process = fake_start_process  # type: ignore[assignment]
    from bash_types import ProcessInfo
    fake_info = ProcessInfo(
        pid=1, command="x", start_time=0, log_file=tmp_path / "fake.log",
        status="completed", exit_code=0,
    )
    tool.process_manager.get_process_info = lambda pid: fake_info  # type: ignore[assignment]
    tool.process_manager.get_summary = lambda pid: {"exit_code": 0, "summary": []}  # type: ignore[assignment]
    tool.process_manager.get_output = lambda pid: "done"  # type: ignore[assignment]

    await tool.execute({"command": "echo hi"})

    assert captured.get("container_id") is None


@pytest.mark.asyncio
async def test_bashtool_execute_with_container_id_skips_security_check(tmp_path):
    """有 _container_id → 跳过内部安全检查（容器内已有独立边界）。

    用 `rm -rf /`（SecurityChecker 判 safe=False 直接拦截）验证：
    无 container_id 时被拦返回 failure，有 container_id 时放行走到 start_process。

    0.2 契约（tool.py _handle_execute 的 is_isolated 分支）：容器隔离
    模式跳过内部 SecurityChecker。
    """
    dangerous_cmd = "rm -rf /"

    # 1) 无 container_id：应被 SecurityChecker 拦截
    tool1 = BashTool()
    result1 = await tool1.execute({"command": dangerous_cmd})
    assert not result1.success, "无 container_id 时 rm -rf / 应被安全检查拦截"
    assert "安全检查" in (result1.error or "") or "SECURITY" in (result1.error_code or "")

    # 2) 有 container_id：应跳过安全检查，直接走到 start_process
    tool2 = BashTool()
    start_called: list = []

    async def fake_start_process(command, working_dir=None, env=None, log_dir=None, container_id=None, provider_kind=None, bwrap_pid=None, owner=None, on_output=None):
        start_called.append(command)
        return 1, tmp_path / "fake.log"

    tool2.process_manager.start_process = fake_start_process  # type: ignore[assignment]
    from bash_types import ProcessInfo
    fake_info = ProcessInfo(
        pid=1, command="x", start_time=0, log_file=tmp_path / "fake.log",
        status="completed", exit_code=0,
    )
    tool2.process_manager.get_process_info = lambda pid: fake_info  # type: ignore[assignment]
    tool2.process_manager.get_summary = lambda pid: {"exit_code": 0, "summary": []}  # type: ignore[assignment]
    tool2.process_manager.get_output = lambda pid: "done"  # type: ignore[assignment]

    result2 = await tool2.execute({"command": dangerous_cmd, "_container_id": "cid"})

    # 有 container_id 时必须真的走到 start_process（跳过了安全检查）
    assert start_called, "有 _container_id 时应跳过安全检查，直接调 start_process"
    assert start_called[0] == dangerous_cmd
    assert result2.success


# ============================================================
# Step 6: 端到端轮询链路（招牌行为：execute→running→continue→completed）
# ============================================================


@pytest.mark.asyncio
async def test_container_execute_running_continue_completed_cycle(tmp_path):
    """完整链路：execute(超时→running) → continue(完成→completed)，pid 一致。

    用真实 ProcessManager + 真实 BashTool.execute，mock 的只有 docker exec
    （返回"还活着"的 fake process）。验证容器路径的轮询行为和本地路径一致。
    """
    tool = BashTool()
    # 容器内 pid=100，host pid=42；进程"还活着"（stream 阻塞，wait 立即返回 0）
    fake_proc = _make_fake_process(host_pid=42, container_pid=100, keep_open=True)

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        # 1) execute：timeout 极小，进程还活着 → 返回 running
        result1 = await tool.execute({
            "command": "sleep 100",
            "_container_id": "cid",
            "timeout": 0.1,  # 极小，立即触发超时
        })

    assert result1.success, f"execute 应成功返回 running，error: {result1.error}"
    data1 = result1.output
    assert data1["status"] == "running", f"第一次应返回 running，实际: {data1['status']}"
    pid = data1["pid"]
    assert pid == 42, f"pid 应是 host pid 42，实际: {pid}"

    # 2) 模拟进程在 continue 期间结束：手动改 status
    info = tool.process_manager.get_process_info(pid)
    assert info is not None
    assert isinstance(info.backend, ContainerProcessBackend)
    assert info.metadata.get("container_pid") == 100
    info.status = "completed"
    info.exit_code = 0
    tool.process_manager.get_summary = lambda p: {"exit_code": 0, "summary": [], "elapsed_seconds": 1.5}  # type: ignore[assignment]
    tool.process_manager.get_output = lambda p: "done"  # type: ignore[assignment]

    # 3) continue：进程已完成 → 返回 completed
    result2 = await tool.execute({
        "action": "continue",
        "pid": pid,
        "timeout": 5,
    })

    assert result2.success, f"continue 应成功，error: {result2.error}"
    data2 = result2.output
    assert data2["status"] == "completed", f"continue 应返回 completed，实际: {data2['status']}"
    assert data2["pid"] == pid, "continue 返回的 pid 应与 execute 一致"
    assert data2["exit_code"] == 0


@pytest.mark.asyncio
async def test_container_execute_timeout_does_not_kill_process(tmp_path):
    """execute 超时返回 running 后，容器内进程未被 docker exec kill（只有 terminate 才杀）。

    验证：mock backend._run_cmd 记录所有调用，execute 超时返回 running 期间
    不应有 kill 命令；只有显式 terminate 才触发 kill。
    """
    tool = BashTool()
    fake_proc = _make_fake_process(host_pid=42, container_pid=100, keep_open=True)

    run_cmd_calls: list = []

    async def fake_run_cmd(args, timeout=30):
        run_cmd_calls.append(args)
        return 0, b"", b""

    with patch(
        "process_manager.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        # execute 超时返回 running
        result = await tool.execute({
            "command": "sleep 100",
            "_container_id": "cid",
            "timeout": 0.1,
        })
    assert result.output["status"] == "running"

    # 注入 mock _run_cmd 到 backend
    info = tool.process_manager.get_process_info(42)
    assert info is not None
    assert isinstance(info.backend, ContainerProcessBackend)
    info.backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]

    # execute 超时期间不应有任何 kill 调用（"kill" 现在在 sh -c 字符串里）
    def _is_kill_call(c: list) -> bool:
        return any("kill" in str(part) for part in c)

    kill_calls = [c for c in run_cmd_calls if _is_kill_call(c)]
    assert not kill_calls, (
        f"execute 超时返回 running 期间不应杀进程，但有 kill 调用: {kill_calls}"
    )

    # 显式 terminate 才触发 kill
    await tool.execute({"action": "terminate", "pid": 42, "_container_id": "cid"})

    kill_calls = [c for c in run_cmd_calls if _is_kill_call(c)]
    assert len(kill_calls) == 1, f"terminate 应触发一次 kill，实际: {kill_calls}"
    args = kill_calls[0]
    assert args[:3] == ["docker", "exec", "cid"]
    cmd_str = " ".join(args)
    assert "100" in cmd_str, "应杀容器内 pid (100)"
