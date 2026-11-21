# @feature: FP-0.2.TB评测适配 | @ci: python-coverage
"""container_probe.py 单元测试（任务容器解析，harbor-free）。

覆盖（≥2 组有区分度输入）：
1. 解析成功路：env.exec 返回 hostname（短 id）→ docker inspect 换正式名 →
   workdir 取 task_env_config.workdir；
2. inspect 失败兜底：短 id 直接作容器 ref（docker exec/inspect 均收短 id）；
3. workdir 缺省：task_env_config 无 workdir / env 无该属性 → /workspace；
4. 显式失败路：exec 失败（rc!=0）→ ContainerProbeError。

隔离策略：environment 桩（鸭子式 exec）；docker CLI（宿主 subprocess）经
monkeypatch 替换 subprocess.run——docker 是外部依赖。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG_DIR = Path(__file__).resolve().parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

from container_probe import (  # noqa: E402
    ContainerProbeError,
    ContainerRef,
    ref_to_dict,
    resolve_container_async,
)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _ExecResult:
    def __init__(self, rc: int, stdout: str, stderr: str = "") -> None:
        self.return_code = rc
        self.stdout = stdout
        self.stderr = stderr


class StubEnv:
    """environment 桩：async exec（真 Harbor 环境形态）+ 可选 task_env_config。"""

    def __init__(self, hostname_rc: int = 0, hostname: str = "abc123def456", workdir: Any = "/app") -> None:
        self._hostname_rc = hostname_rc
        self._hostname = hostname
        if workdir is not None:
            cfg = types.SimpleNamespace(workdir=workdir)
            self.task_env_config = cfg
        self.exec_calls: list[str] = []

    async def exec(self, command: str, timeout_sec: int | None = None) -> _ExecResult:
        self.exec_calls.append(command)
        return _ExecResult(self._hostname_rc, self._hostname, "" if self._hostname_rc == 0 else "exec failed")


def _patch_inspect(monkeypatch: pytest.MonkeyPatch, name_or_none: str | None) -> None:
    """替换 docker inspect（宿主 subprocess）：返回带前导斜杠的 .Name 或 None。"""

    def _fake_run(args: Any, capture_output: Any = True, timeout: Any = None, check: Any = False) -> Any:
        assert args[0:2] == ["docker", "inspect"]
        if name_or_none is None:
            return subprocess.CompletedProcess(args, 1, b"", b"Error: no such object")
        return subprocess.CompletedProcess(args, 0, f"/{name_or_none}\n".encode(), b"")

    monkeypatch.setattr("container_probe.subprocess.run", _fake_run)


class TestResolveSuccess:
    def test_hostname_plus_inspect_full_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = StubEnv(hostname="abc123def456", workdir="/app")
        _patch_inspect(monkeypatch, "tb-main-1")
        ref = _run(resolve_container_async(env))
        assert isinstance(ref, ContainerRef)
        assert ref.name == "tb-main-1"
        assert ref.workdir == "/app"
        assert env.exec_calls == ["cat /etc/hostname"]

    def test_inspect_failure_falls_back_to_short_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = StubEnv(hostname="abc123def456", workdir="/")
        _patch_inspect(monkeypatch, None)
        ref = _run(resolve_container_async(env))
        assert ref.name == "abc123def456"
        assert ref.workdir == "/"

    def test_workdir_defaults_to_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = StubEnv(workdir=None)  # 无 task_env_config 属性
        _patch_inspect(monkeypatch, "tb-x")
        ref = _run(resolve_container_async(env))
        assert ref.workdir == "/workspace"

    def test_workdir_blank_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = StubEnv(workdir="   ")  # 显式空白同缺省
        _patch_inspect(monkeypatch, "tb-x")
        assert _run(resolve_container_async(env)).workdir == "/workspace"


class TestResolveFailure:
    def test_exec_failure_explicit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = StubEnv(hostname_rc=1)
        with pytest.raises(ContainerProbeError, match="容器自证失败"):
            _run(resolve_container_async(env))

    def test_empty_hostname_explicit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = StubEnv(hostname="   ")
        with pytest.raises(ContainerProbeError, match="容器自证失败"):
            _run(resolve_container_async(env))


class TestMisc:
    def test_ref_to_dict(self) -> None:
        assert ref_to_dict(ContainerRef(name="tb-1", workdir="/app")) == {
            "name": "tb-1",
            "workdir": "/app",
        }
