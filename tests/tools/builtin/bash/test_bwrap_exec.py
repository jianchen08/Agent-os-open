"""BwrapProcessBackend 单元测试（TDD / M3）。

对称于 ContainerProcessBackend（docker exec 路径），但用 nsenter 注入：
- ContainerProcessBackend.kill → docker exec <cid> sh -c 'kill -9 <pid>'
- BwrapProcessBackend.kill    → nsenter -t <bwrap_pid> ... sh -c 'kill -9 <pid>'

bwrap 的"环境标识"是 PID 1（bwrap_pid，即 create_environment 记下的常驻进程），
而非 docker 容器名。命令注入（nsenter --target <bwrap_pid>）和进程杀（同样经
nsenter 进 namespace 后 kill 容器内 pid）都靠它定位。

本测试不依赖真实 bwrap/nsenter —— 全部 mock _run_cmd（与 test_container_exec.py 同惯例）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.builtin.bash.process_manager import BwrapProcessBackend, ContainerProcessBackend
from tools.builtin.bash.types import WorkUnit


# ---------------------------------------------------------------------------
# 1. kill：nsenter 注入单进程杀（对称 docker exec kill）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bwrap_backend_kill_uses_nsenter_single_pid():
    """kill 经 nsenter 进入 bwrap namespace，单进程杀（不整组，与 ContainerBackend 同语义）。"""
    backend = BwrapProcessBackend(bwrap_pid=12345, container_id="cua-ws")
    captured: list = []

    async def fake_run_cmd(args, timeout=30):
        captured.append(args)
        return 0, b"", b""

    backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]
    await backend.kill(WorkUnit(pid=100, command="x", metadata={"container_id": "cua-ws"}))

    args = captured[0]
    # nsenter 定位到 bwrap PID 1 的 namespace
    assert args[0] == "nsenter"
    assert "--target" in args
    assert "12345" in args
    # 命令是 kill -9 <容器内pid>
    joined = " ".join(args)
    assert "kill -9" in joined
    assert " 100" in joined  # 容器内 pid


@pytest.mark.asyncio
async def test_bwrap_backend_kill_sigterm_when_not_force():
    """force=False → SIGTERM (-15)。"""
    backend = BwrapProcessBackend(bwrap_pid=99, container_id="cua-x")

    captured: list = []

    async def fake_run_cmd(args, timeout=30):
        captured.append(args)
        return 0, b"", b""

    backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]
    await backend.kill(WorkUnit(pid=200, command="x"), force=False)

    joined = " ".join(captured[0])
    assert "kill -15" in joined


@pytest.mark.asyncio
async def test_bwrap_backend_kill_swallows_error_when_process_gone():
    """kill 命令失败（进程已退出）→ 不抛（与 ContainerBackend 同：已退出是正常的）。"""
    backend = BwrapProcessBackend(bwrap_pid=99, container_id="cua-x")

    async def fake_run_cmd(args, timeout=30):
        raise RuntimeError("nsenter failed: process does not exist")

    backend._run_cmd = fake_run_cmd  # type: ignore[attr-defined]
    # 不应抛
    await backend.kill(WorkUnit(pid=200, command="x"))


# ---------------------------------------------------------------------------
# 2. 内存采样：与 ContainerBackend 一致，返回 None（沙箱无 cgroup OOM 兜底）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bwrap_backend_sample_memory_returns_none():
    """bwrap 无 cgroup 内存控制（v1 advisory），宿主侧采样无意义 → None。"""
    backend = BwrapProcessBackend(bwrap_pid=1, container_id="c")
    assert await backend.sample_memory() is None
    assert await backend.sample_unit_memory(WorkUnit(pid=1, command="x")) is None


# ---------------------------------------------------------------------------
# 3. exec_argv：nsenter 命令注入 argv（start_process 用）
# ---------------------------------------------------------------------------


def test_bwrap_backend_exec_argv_for_command():
    """_exec_argv 构造 nsenter 注入命令的 argv（start_process 调它替代写死 docker exec）。"""
    backend = BwrapProcessBackend(bwrap_pid=4242, container_id="cua-ws")
    argv = backend._exec_argv(workdir="/workspace", command="echo $$; exec ls")

    assert argv[0] == "nsenter"
    assert "--target" in argv
    assert "4242" in argv
    # 命令经 sh -c 执行
    joined = " ".join(argv)
    assert "echo $$; exec ls" in joined
    assert "/workspace" in joined


# ---------------------------------------------------------------------------
# 4. 工厂：按 provider_kind 分发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_process_backend_docker_for_docker_kind():
    """provider_kind=docker（或缺失）→ ContainerProcessBackend（现有行为）。"""
    from tools.builtin.bash.process_manager import _get_process_backend

    backend = _get_process_backend(
        container_id="cua-docker", provider_kind="docker", bwrap_pid=None
    )
    assert isinstance(backend, ContainerProcessBackend)


@pytest.mark.asyncio
async def test_get_process_backend_bwrap_for_bwrap_kind():
    """provider_kind=bwrap + bwrap_pid → BwrapProcessBackend。"""
    from tools.builtin.bash.process_manager import _get_process_backend

    backend = _get_process_backend(
        container_id="cua-ws", provider_kind="bwrap", bwrap_pid=12345
    )
    assert isinstance(backend, BwrapProcessBackend)


@pytest.mark.asyncio
async def test_get_process_backend_bwrap_cached_by_container_id():
    """同 container_id 复用同一 backend 实例（与 _get_container_backend 同缓存语义）。"""
    from tools.builtin.bash.process_manager import _get_process_backend

    b1 = _get_process_backend(container_id="cid-a", provider_kind="bwrap", bwrap_pid=1)
    b2 = _get_process_backend(container_id="cid-a", provider_kind="bwrap", bwrap_pid=1)
    assert b1 is b2


# ---------------------------------------------------------------------------
# 5. start_process：provider_kind=bwrap 时走 nsenter（而非 docker exec）
# ---------------------------------------------------------------------------


def _make_fake_process(host_pid=42, container_pid=100):
    """构造假 asyncio.subprocess.Process（仿 test_container_exec._make_fake_process）。"""
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.pid = host_pid
    fake.returncode = None

    class _FakeStream:
        def __init__(self, first_line: bytes = b"") -> None:
            self._buf = first_line

        async def readline(self) -> bytes:
            line, self._buf = self._buf, b""
            return line

        async def read(self, n: int = -1) -> bytes:
            data, self._buf = self._buf, b""
            return data

    first_line = f"{container_pid}\n".encode()
    fake.stdout = _FakeStream(first_line)
    fake.stderr = _FakeStream()
    fake.wait = AsyncMock()
    fake.kill = MagicMock()
    fake.terminate = MagicMock()
    return fake


@pytest.mark.asyncio
async def test_start_process_bwrap_uses_nsenter_not_docker(tmp_path):
    """start_process 传 provider_kind=bwrap + bwrap_pid → create_subprocess_exec 收到 nsenter。"""
    from unittest.mock import patch

    from tools.builtin.bash.process_manager import ProcessManager

    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=42, container_pid=100)

    captured: list = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.extend(args)
        return fake_proc

    with patch(
        "tools.builtin.bash.process_manager.asyncio.create_subprocess_exec",
        side_effect=fake_create_subprocess_exec,
    ):
        await pm.start_process(
            command="echo hi",
            working_dir="/workspace",
            container_id="cua-ws",
            provider_kind="bwrap",
            bwrap_pid=5555,
        )

    # captured 是平铺的 argv（extend 进单个 list）
    assert captured[0] == "nsenter"
    assert "--target" in captured
    assert "5555" in captured
    # 不应出现 docker
    assert "docker" not in captured


@pytest.mark.asyncio
async def test_start_process_docker_kind_unchanged(tmp_path):
    """provider_kind=docker（或缺失）→ 仍走 docker exec（回归保护）。"""
    from unittest.mock import patch

    from tools.builtin.bash.process_manager import ProcessManager

    pm = ProcessManager(log_dir=tmp_path / "logs")
    fake_proc = _make_fake_process(host_pid=1, container_pid=2)

    captured: list = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.extend(args)
        return fake_proc

    with patch(
        "tools.builtin.bash.process_manager.asyncio.create_subprocess_exec",
        side_effect=fake_create_subprocess_exec,
    ):
        await pm.start_process(
            command="ls",
            working_dir="/workspace",
            container_id="abc",
            provider_kind="docker",
        )

    assert captured[0] == "docker"
