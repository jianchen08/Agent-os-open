# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""process_manager wsl_native 后端接线测试。

锁定契约：
- start_process 新增 exec_backend 通道：backend=wsl_native 时以 wsl 传输前缀
  起 wsl.exe 进程（--cd 映射后的 workspace、env/sandbox 前缀、sh -c 包装），
  pid 读取协议（echo $$ 第一行）与 docker 路径一致，metadata 携带 exec_backend。
- 杀进程后端：wsl_native 走 wsl 传输 + sh 内建 kill 单进程杀，不做宿主内存采样
  （与容器后端同纪律）。
- docker 路径不回归：无 exec_backend 时 argv 仍以 docker exec 起进程。
- tool.py 透传：execute 的 _exec_backend（guard 服务端注入）原样传给
  start_process。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

_BASH_DIR = str(Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tools" / "bash")
if _BASH_DIR not in sys.path:
    sys.path.insert(0, _BASH_DIR)

from process_manager import ProcessManager  # noqa: E402

pytestmark = pytest.mark.unit

_BACKEND_BASE: dict[str, Any] = {
    "backend": "wsl_native",
    "wsl_exe": "wsl",
    "distro": "Ubuntu",
    "user": None,
    "sandbox_cmd": [],
    "workspace_wsl": "/mnt/d/ws",
}


class _FakeStream:
    def __init__(self, first_line: bytes) -> None:
        self._first = first_line

    async def readline(self) -> bytes:
        return self._first


class _FakeProcess:
    def __init__(self, pid: int, first_line: bytes) -> None:
        self.pid = pid
        self.stdout = _FakeStream(first_line)
        self.stderr = AsyncMock()
        self.stdin = AsyncMock()

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _patch_exec(monkeypatch: pytest.MonkeyPatch, first_line: bytes = b"4321\n") -> list[list[str]]:
    """替换 asyncio.create_subprocess_exec，捕获 argv，返回假进程。"""
    argvs: list[list[str]] = []

    def fake_exec(*args: Any, **kwargs: Any) -> Any:
        argvs.append(list(args))
        return _FakeProcess(pid=9999, first_line=first_line)

    async def async_exec(*args: Any, **kwargs: Any) -> Any:
        return fake_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", async_exec)
    return argvs


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestWslNativeStart:
    def test_start_argv_shape_and_metadata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        pid, log_file = _run(
            pm.start_process(
                "cargo build",
                working_dir="/workspace",
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        assert pid == 9999
        assert argvs, "wsl 进程未被启动"
        argv = argvs[0]
        # 传输前缀 + --cd 映射 + sh -c 包装（echo $$; exec <cmd>）
        assert argv[:2] == ["wsl", "-d"]
        assert "Ubuntu" in argv
        assert "-u" not in argv  # user None
        assert argv[argv.index("--cd") + 1] == "/mnt/d/ws"
        assert argv[-3:-1] == ["bash", "-c"]
        assert argv[-1] == "echo $$; exec cargo build"
        # metadata：容器 pid 协议与 docker 路径一致
        info = pm.active_processes.get(9999)
        assert info is not None
        assert info.metadata["container_id"] == "cua-ws1"
        assert info.metadata["container_pid"] == 4321
        assert info.metadata["exec_backend"]["backend"] == "wsl_native"

    def test_start_with_user_sandbox_and_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        argvs = _patch_exec(monkeypatch)
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://1.2.3.4:8765")
        monkeypatch.setenv("AGENTOS_BRIDGE_TOKEN", "tok")
        pm = ProcessManager()
        backend = dict(_BACKEND_BASE, user="agentos", sandbox_cmd=["bwrap", "--dev", "/dev"])
        _run(
            pm.start_process(
                "ls",
                working_dir=None,
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=backend,
            )
        )
        argv = argvs[0]
        assert argv[argv.index("-u") + 1] == "agentos"
        assert argv[argv.index("--cd") + 1] == "/mnt/d/ws"  # None → workspace_wsl
        after_sep = argv[argv.index("--exec") + 1:]
        assert after_sep[0] == "env"
        assert "AGENTOS_BRIDGE_URL=http://1.2.3.4:8765" in after_sep
        assert "AGENTOS_BRIDGE_TOKEN=tok" in after_sep
        assert "bwrap" in after_sep

    def test_working_dir_backslash_workspace_mapped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 工具层 ntpath 规整产物 \workspace → 应映射到 workspace_wsl（2026-09-14 管道实测 E_INVALIDARG 根因）
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "ls -la",
                working_dir="\\workspace",
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        assert argvs[0][argvs[0].index("--cd") + 1] == "/mnt/d/ws"

    def test_working_dir_windows_path_passthrough(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "ls",
                working_dir="D:\\host\\dir",
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        argv = argvs[0]
        assert argv[argv.index("--cd") + 1] == "D:\\host\\dir"

    def test_compound_command_wrapped_as_inner_sh(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                'echo "标签" && ls',
                working_dir="/workspace",
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        wrapped = argvs[0][-1]
        assert wrapped.startswith("echo $$; exec bash -c ")


class TestWslNativeKillBackend:
    def test_kill_uses_wsl_transport(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "sleep 100",
                working_dir="/workspace",
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        info = pm.active_processes[9999]
        calls: list[list[str]] = []

        async def fake_run_cmd(args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
            calls.append(list(args))
            return 0, b"", b""

        info.backend._run_cmd = fake_run_cmd  # type: ignore[method-assign]
        unit = AsyncMock()
        unit.pid = 4321
        unit.metadata = {"container_id": "cua-ws1"}
        _run(info.backend.kill(unit, force=True))
        assert calls == [["wsl", "-d", "Ubuntu", "--exec", "bash", "-c", "kill -9 4321 2>/dev/null || true"]]

    def test_memory_sampling_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "sleep 100",
                working_dir="/workspace",
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        info = pm.active_processes[9999]
        assert _run(info.backend.sample_memory()) is None
        unit = AsyncMock()
        unit.pid = 4321
        unit.metadata = {"container_id": "cua-ws1"}
        assert _run(info.backend.sample_unit_memory(unit)) is None


class TestDockerPathUnchanged:
    def test_no_exec_backend_keeps_docker_exec(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "ls",
                working_dir=None,
                log_dir=tmp_path,
                container_id="cua-ws1",
            )
        )
        argv = argvs[0]
        assert argv[0] == "docker"
        assert argv[:2] == ["docker", "exec"]


class _SentinelPM:
    """实例级 start_process 替身：记录调用参数后以哨兵异常中断执行流。

    不打类级补丁——同进程多插件裸名模块可能存在多份类对象，实例级替换
    与类身份无关，合跑稳定。
    """

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def start_process(self, command: str, **kwargs: Any) -> tuple[int, Path]:
        self._captured["command"] = command
        self._captured.update(kwargs)
        raise RuntimeError("sentinel-stop")


class TestToolPassthrough:
    def test_execute_forwards_exec_backend(self, tmp_path: Path) -> None:
        from tool import BashTool

        captured: dict[str, Any] = {}
        tool = BashTool()
        tool.process_manager = _SentinelPM(captured)  # type: ignore[assignment]
        _run(
            tool.execute(
                {
                    "action": "execute",
                    "command": "ls",
                    "_container_id": "cua-ws1",
                    "_exec_backend": dict(_BACKEND_BASE),
                }
            )
        )
        assert captured.get("exec_backend") == dict(_BACKEND_BASE)
        assert captured.get("container_id") == "cua-ws1"

    def test_exec_backend_ignored_without_container_id(self, tmp_path: Path) -> None:
        from tool import BashTool

        captured: dict[str, Any] = {}
        tool = BashTool()
        tool.process_manager = _SentinelPM(captured)  # type: ignore[assignment]
        _run(
            tool.execute(
                {
                    "action": "execute",
                    "command": "ls",
                    "_exec_backend": dict(_BACKEND_BASE),
                }
            )
        )
        # 非隔离路径：不携带 exec_backend（声明式标记不构成隔离凭据）
        assert captured.get("exec_backend") is None
