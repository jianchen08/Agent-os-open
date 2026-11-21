# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""process_manager mxc_native 后端接线测试。

锁定契约：
- start_process 分派：exec_backend.backend=mxc_native 时以
  ``[executor] + executor_args``（``{request}`` → 请求 JSON 路径）起一次性
  执行器子进程；
- 请求形状 = MXC 1.0.0 stable schema（version/process/containment/
  filesystem/network/lifecycle；wslc 档带 image）；无 echo $$ pid 上报；
- 敏感 env（AGENTOS_BRIDGE_TOKEN）不写入请求档（请求落盘）；
- 无首行门控（gate 关闭），失败不自动重试；
- 杀灭/内存采样走宿主执行器进程树（MxcNativeProcessBackend 继承本地后端）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

_BASH_DIR = str(Path(__file__).resolve().parents[4] / "plugins" / "shared" / "bash")
if _BASH_DIR not in sys.path:
    sys.path.insert(0, _BASH_DIR)

from agentos_plugin_sdk.process_runtime import ProcessManager  # noqa: E402
from agentos_plugin_sdk.process_runtime.backends import (  # noqa: E402
    MxcNativeProcessBackend,
    _get_mxc_native_backend,
)
from agentos_plugin_sdk.process_runtime.transports import (  # noqa: E402
    _build_mxc_request,
    _map_mxc_working_dir,
)

pytestmark = pytest.mark.unit

_EXECUTOR = "C:\\mxc\\wxc-exec.exe"

_BACKEND_BASE: dict[str, Any] = {
    "backend": "mxc_native",
    "profile": "linux-bash",
    "mxc_backend": "wslc",
    "executor": _EXECUTOR,
    "executor_args": ["{request}"],
    "request_version": "1.0.0",
    "workspace": "C:\\ws",
    "policy": {
        "readwritePaths": ["C:\\ws"],
        "readonlyPaths": [],
        "deniedPaths": ["C:\\Users\\u\\.ssh"],
    },
    "network": {"egress": {"default": "deny"}},
    "image": "agentos-linux:latest",
    "env_name": "cua-ws",
}


class _FakeStream:
    def __init__(self) -> None:
        self._eof = False

    async def readline(self) -> bytes:
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
    argvs: list[list[str]] = []

    async def async_exec(*args: Any, **kwargs: Any) -> Any:
        argvs.append([str(a) for a in args])
        return _FakeProcess(pid=4242)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", async_exec)
    return argvs


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestStartMxcNative:
    def test_start_spawns_executor_with_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argvs = _patch_exec(monkeypatch)
        pm = ProcessManager()
        pid, log_file = _run(
            pm.start_process(
                "cargo build",
                working_dir="/workspace",
                log_dir=tmp_path,
                container_id="cua-ws",
                exec_backend=dict(_BACKEND_BASE),
            )
        )
        assert pid == 4242
        assert log_file.exists()
        assert argvs, "MXC 执行器未被启动"
        argv = argvs[0]
        assert argv[0] == _EXECUTOR
        request_path = Path(argv[1])
        assert request_path.is_file()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert request["version"] == "1.0.0"
        assert request["containment"] == "wslc"
        assert request["process"]["commandLine"].endswith("cargo build")
        assert request["process"]["cwd"] == "C:\\ws"
        assert "timeout" not in request["process"]
        assert request["filesystem"]["readwritePaths"] == ["C:\\ws"]
        assert request["network"]["egress"]["default"] == "deny"
        assert request["wslc"]["image"] == "agentos-linux:latest"
        assert request["lifecycle"] == {"destroyOnExit": True}
        # 敏感 env 不落请求档；非敏感缓存导向仍在
        env_pairs = request["process"]["env"]
        assert all("AGENTOS_BRIDGE_TOKEN" not in pair for pair in env_pairs)
        assert any("npm_config_cache" in pair for pair in env_pairs)
        # 注册元数据带请求档路径（收尾清理锚）
        info = pm.active_processes[pid]
        assert info.metadata["mxc_request"] == str(request_path)

    def test_native_win_request_omits_wslc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argvs = _patch_exec(monkeypatch)
        backend = dict(_BACKEND_BASE)
        backend.update(
            {
                "profile": "native-win",
                "mxc_backend": "processcontainer",
                "workspace": "C:\\ws",
                "image": None,
            }
        )
        pm = ProcessManager()
        _run(
            pm.start_process(
                "powershell -Command Get-ChildItem",
                working_dir="C:\\ws",
                log_dir=tmp_path,
                container_id="cua-ws--native-win",
                exec_backend=backend,
            )
        )
        request = json.loads(Path(argvs[0][1]).read_text(encoding="utf-8"))
        assert request["containment"] == "processcontainer"
        assert "wslc" not in request
        # 原生 Windows 命令按 cmd 语义包装（MXC 不套 shell）；Win32k 锁死会
        # 让控制台程序 DLL 初始化失败，ui.disable 必须放开。
        assert request["process"]["commandLine"] == (
            'cmd.exe /d /s /c "powershell -Command Get-ChildItem"'
        )
        assert request["ui"] == {"disable": False}

    def test_backend_cache_by_config(self) -> None:
        first = _get_mxc_native_backend(dict(_BACKEND_BASE))
        second = _get_mxc_native_backend(dict(_BACKEND_BASE))
        assert first is second
        assert isinstance(first, MxcNativeProcessBackend)


class TestMxcRequestHelpers:
    def test_map_workspace_paths(self) -> None:
        backend = {"workspace": "/host/ws"}
        assert _map_mxc_working_dir(backend, "/workspace") == "/host/ws"
        assert _map_mxc_working_dir(backend, "/workspace/sub") == "/host/ws/sub"
        assert _map_mxc_working_dir(backend, "frontend") == "/host/ws/frontend"
        assert _map_mxc_working_dir(backend, "D:\\abs") == "D:\\abs"
        assert _map_mxc_working_dir(backend, None) == "/host/ws"

    def test_request_timeout_included_when_positive(self) -> None:
        request = _build_mxc_request(dict(_BACKEND_BASE), "echo hi", "/workspace", 12.5)
        assert request["process"]["timeout"] == 12500

    def test_request_env_empty_without_workspace(self) -> None:
        backend = dict(_BACKEND_BASE, workspace="")
        request = _build_mxc_request(backend, "echo hi", None, 0.0)
        assert request["process"]["env"] == []
        assert request["process"]["cwd"] == ""


class TestMxcBackendHealth:
    def test_probe_health_executor_missing(self) -> None:
        backend = MxcNativeProcessBackend({"executor": ""})
        assert _run(backend.probe_health()) is False

    def test_probe_health_executor_present(self, tmp_path: Path) -> None:
        exe = tmp_path / "wxc-exec.exe"
        exe.write_text("", encoding="utf-8")
        backend = MxcNativeProcessBackend({"executor": str(exe)})
        assert _run(backend.probe_health()) is True
