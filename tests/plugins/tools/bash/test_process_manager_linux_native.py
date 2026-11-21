# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""process_manager linux_native 后端接线测试。

锁定契约：
- start_process 分派：exec_backend.backend=linux_native 时以 sandbox_prefix
  （provider 烘好的 bwrap 选项段，原样透传）+ --chdir + -- + bash -c 起沙箱
  进程；bridge/runtime env 不在本侧处理（provider 已 --setenv 烘入 prefix）。
- pid 协议差异：无 echo $$ 上报行（--unshare-pid 下内层 pid 宿主不可寻址，
  上报反而危险）；metadata 不含 container_pid → terminate/看门狗按宿主 pid
  杀（本地后端 psutil 整树杀，pid-1 退出内核回收整 ns）。
- working_dir 映射与 LinuxNativeProvider._map_working_dir 同语义（漂移钉）。
- tool.py 透传：_exec_backend（guard 服务端注入）原样传给 start_process。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

_BASH_DIR = str(Path(__file__).resolve().parents[4] / "plugins" / "shared" / "bash")
if _BASH_DIR not in sys.path:
    sys.path.insert(0, _BASH_DIR)

from agentos_plugin_sdk.process_runtime import LocalProcessBackend, ProcessManager  # noqa: E402

pytestmark = pytest.mark.unit

_SANDBOX_PREFIX: list[str] = [
    "bwrap",
    "--ro-bind", "/", "/",
    "--bind", "/host/ws", "/workspace",
    "--proc", "/proc",
    "--dev", "/dev",
    "--tmpfs", "/tmp",
    "--unshare-pid",
    "--die-with-parent",
    "--unshare-net",
]

_BACKEND_BASE: dict[str, Any] = {
    "backend": "linux_native",
    "bwrap_path": "bwrap",
    "network": False,
    "state_dir": "/state",
    "workspace": "/host/ws",
    "sandbox_prefix": list(_SANDBOX_PREFIX),
}


class _FakeStream:
    def __init__(self) -> None:
        self._eof = False

    async def readline(self) -> bytes:
        # 立即 EOF：linux_native 无 pid 上报行，输出读取任务直接收尾
        if self._eof:
            return b""
        self._eof = True
        return b""


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.stdout = _FakeStream()
        self.stderr = AsyncMock()
        self.stdin = AsyncMock()

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _patch_exec(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """替换 asyncio.create_subprocess_exec，捕获 argv，返回假进程。"""
    argvs: list[list[str]] = []

    def fake_exec(*args: Any, **kwargs: Any) -> Any:
        argvs.append(list(args))
        return _FakeProcess(pid=9999)

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


class TestLinuxNativeStart:
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
        assert argvs, "bwrap 沙箱进程未被启动"
        argv = argvs[0]
        # sandbox_prefix 原样透传（模板单点在 provider，本侧不重建）
        assert argv[: len(_SANDBOX_PREFIX)] == _SANDBOX_PREFIX
        assert argv[argv.index("--chdir") + 1] == "/host/ws"  # /workspace -> workspace 真实路径
        assert argv[-4:-1] == ["--", "bash", "-c"]
        # 无 pid 上报协议：包装不含 echo $$
        assert argv[-1] == "exec cargo build"
        # metadata：无 container_pid（宿主 pid 即杀灭键）+ exec_backend 携带
        info = pm.active_processes.get(9999)
        assert info is not None
        assert info.metadata["container_id"] == "cua-ws1"
        assert "container_pid" not in info.metadata
        assert info.metadata["exec_backend"]["backend"] == "linux_native"

    def test_owner_recorded_in_metadata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "ls",
                working_dir=None,
                log_dir=tmp_path,
                container_id="cua-ws1",
                owner="session-1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        info = pm.active_processes[9999]
        assert info.metadata["owner"] == "session-1"

    def test_backend_is_local_psutil_tree_kill(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """杀灭走宿主侧本地后端（psutil 整树杀）——沙箱即宿主进程树。"""
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "sleep 100",
                working_dir=None,
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        info = pm.active_processes[9999]
        assert isinstance(info.backend, LocalProcessBackend)

    def test_working_dir_mapping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """绝对 POSIX 原样；/workspace 约定与空/相对落 workspace 真实路径。"""
        ws = _BACKEND_BASE["workspace"]
        for wd, expected in ((None, ws), ("", ws), ("sub/dir", ws), ("/workspace/sub", ws + "/sub"), ("/tmp/other", "/tmp/other")):
            argvs = _patch_exec(monkeypatch)
            pm = ProcessManager()
            _run(
                pm.start_process(
                    "ls",
                    working_dir=wd,
                    log_dir=tmp_path,
                    container_id="cua-ws1",
                    exec_backend=dict(_BACKEND_BASE),
                )
            )
            assert argvs[0][argvs[0].index("--chdir") + 1] == expected, f"wd={wd!r}"

    def test_compound_command_wrapped_as_inner_bash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        # 复合命令套内层 bash（pipefail 与容器/wsl 路径同语义），仍无 echo $$
        assert wrapped.startswith("exec bash -c ")
        assert "echo $$" not in wrapped
        assert "pipefail" in wrapped

    def test_no_env_handling_on_bash_side(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """bridge env 由 provider --setenv 烘进 prefix，bash 侧不做 env 前缀。"""
        monkeypatch.setenv("AGENTOS_BRIDGE_URL", "http://1.2.3.4:8765")
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        _run(
            pm.start_process(
                "ls",
                working_dir=None,
                log_dir=tmp_path,
                container_id="cua-ws1",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        argv = argvs[0]
        assert "env" not in argv
        assert not any(a.startswith("AGENTOS_BRIDGE_URL=") for a in argv)

    def test_working_dir_mapping_matches_provider(self, tmp_path: Path) -> None:
        """漂移钉：bash 侧映射与 LinuxNativeProvider._map_working_dir 同语义。"""
        import importlib.util

        _ISO_PROVIDERS = (
            Path(__file__).resolve().parents[4] / "plugins" / "shared" / "isolation" / "providers"
        )
        # provider 模块自持解析需 isolation 目录在 sys.path（providers 包定位）
        _ISO_DIR = str(_ISO_PROVIDERS.parent)
        if _ISO_DIR not in sys.path:
            sys.path.insert(0, _ISO_DIR)
        mod_name = "linux_native_provider_bash_pin"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(mod_name, _ISO_PROVIDERS / "linux_native_provider.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        provider = module.LinuxNativeProvider({"state_dir": str(tmp_path), "workspace": "/host/ws"})
        from agentos_plugin_sdk.process_runtime.transports import _map_linux_native_working_dir

        for wd in ("/workspace", "/workspace/sub", "/tmp/other", None, "", "relative/x", "D:\\host\\dir"):
            assert _map_linux_native_working_dir(dict(_BACKEND_BASE), wd) == provider._map_working_dir(wd), f"wd={wd!r}"


class TestToolPassthrough:
    def test_execute_forwards_linux_native_backend(self, tmp_path: Path) -> None:
        from tool import BashTool

        captured: dict[str, Any] = {}

        class _SentinelPM:
            async def start_process(self, command: str, **kwargs: Any) -> tuple[int, Path]:
                captured["command"] = command
                captured.update(kwargs)
                raise RuntimeError("sentinel-stop")

        tool = BashTool()
        tool.process_manager = _SentinelPM()  # type: ignore[assignment]
        _run(
            tool.run_command(
                {
                    "command": "ls",
                    "_container_id": "cua-ws1",
                    "_exec_backend": dict(_BACKEND_BASE),
                }
            )
        )
        assert captured.get("exec_backend") == dict(_BACKEND_BASE)
        assert captured.get("container_id") == "cua-ws1"
