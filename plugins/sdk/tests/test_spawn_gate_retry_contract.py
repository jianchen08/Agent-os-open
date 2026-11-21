# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""spawn 首行门控 + 后端健康探测重试契约（2026-11-17 WSL 挂死事故直修）。

事故实证：WSLService 卡死后，每次 bash 调用 spawn 的 ``wsl.exe`` 客户端阻塞在
传输层、永不产出 ``echo $$`` 首行 —— 工具侧只有 30 分钟孤儿兜底，而兜底 kill
又依赖同一传输通道（``wsl.exe ... bash -c 'kill -9'``），失败被吞，348 个
``wsl.exe`` 客户端累积把会话通道堵死。

本文件钉四条契约：

1. **首行门控**：spawn 后 N 秒拿不到 pid 行 = 命令未确认启动 →
   宿主级杀树（``kill_host_tree``，绝不经后端传输通道去 kill）；
2. **探测门控重试**：健康探测 OK → 重试一次（同一条命令语义）；
   探测失败/不支持 → 不重试；
3. **快速失败 + 熔断**：探测失败进入冷却期，后续调用不再 spawn；
4. **收到过首行 = 命令已启动**：永不自动重试（防副作用双执行）。

测试桩纪律：asyncio 进程用可编程桩（真实 StreamReader 驱动 reader 任务）；
ProcessBackend 用最小假后端记录 kill / kill_host_tree / probe_health 调用面。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.process_runtime import ProcessManager
from agentos_plugin_sdk.process_runtime import manager as manager_module
from agentos_plugin_sdk.process_runtime.manager import (
    BackendUnhealthyError,
    SpawnFirstLineTimeoutError,
)

pytestmark = pytest.mark.unit


# ─────────────────────────── 桩 ───────────────────────────


class _StubStream:
    """asyncio.StreamReader 替身：可喂数据/EOF，reader 任务真实消费。"""

    def __init__(self) -> None:
        self._reader = asyncio.StreamReader()

    def feed(self, data: bytes) -> None:
        self._reader.feed_data(data)

    def eof(self) -> None:
        self._reader.feed_eof()

    async def readline(self) -> bytes:
        return await self._reader.readline()

    async def read(self, n: int = -1) -> bytes:
        return await self._reader.read(n)


class _StubProcess:
    """最小 asyncio 进程桩：stdout 可编程（是否产出首行）、wait 事件驱动。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.stdout = _StubStream()
        self.stderr = _StubStream()
        self.stdin: Any = None
        self.returncode: int | None = None
        self._exit = asyncio.Event()
        self._code = 0

    def emit(self, data: bytes) -> None:
        self.stdout.feed(data)

    def die(self, code: int = 0) -> None:
        if self._exit.is_set():
            return
        self._code = code
        self.returncode = code
        self.stdout.eof()
        self.stderr.eof()
        self._exit.set()

    def kill(self) -> None:
        self.die(1)

    async def wait(self) -> int:
        await self._exit.wait()
        return self._code


class _FakeWslBackend:
    """假 wsl_native 后端：记录三类杀法调用面 + 可编程健康探测结果。"""

    def __init__(self, *, probe: bool | None, procs: dict[int, _StubProcess]) -> None:
        self._probe = probe
        self._procs = procs
        self.probe_calls = 0
        self.host_killed: list[int] = []
        self.kill_calls: list[int] = []

    async def kill(self, unit: Any, force: bool = True) -> None:
        self.kill_calls.append(unit.pid)
        proc = self._procs.get(unit.pid)
        if proc is not None:
            proc.die(137 if force else 143)

    async def kill_host_tree(self, host_pid: int) -> None:
        self.host_killed.append(host_pid)
        proc = self._procs.get(host_pid)
        if proc is not None:
            proc.die(1)

    async def probe_health(self) -> bool | None:
        self.probe_calls += 1
        return self._probe

    async def sample_memory(self) -> float | None:
        return None

    async def sample_unit_memory(self, unit: Any) -> int | None:
        return None


def _exec_backend() -> dict[str, Any]:
    return {"backend": "wsl_native", "distro": "Ubuntu", "workspace_wsl": "/workspace"}


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    backend: _FakeWslBackend,
    procs: list[_StubProcess],
) -> list[_StubProcess]:
    """接线：按序返回桩进程 + 后端工厂替换为假后端。返回 spawn 调用台账。"""
    spawned: list[_StubProcess] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _StubProcess:
        proc = procs[len(spawned)]
        spawned.append(proc)
        return proc

    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(manager_module, "_get_wsl_native_backend", lambda _b: backend)
    return spawned


def _make_manager(tmp_path: Path) -> ProcessManager:
    return ProcessManager(
        log_dir=tmp_path / "logs",
        spawn_first_line_timeout=0.05,
        spawn_max_attempts=2,
        backend_unhealthy_cooldown_secs=60.0,
    )


# ─────────────────── 契约 1+2：超时 → 宿主级杀树 → 探测 OK 重试 ───────────────────


class TestSpawnGateRetry:
    async def test_timeout_kills_host_tree_and_retries_when_probe_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _StubProcess(1001)  # 永不出首行（模拟 wsl.exe 阻塞在传输层）
        second = _StubProcess(1002)
        second.emit(b"4321\n")  # 重试成功：pid 行到达，进程保持 running
        backend = _FakeWslBackend(probe=True, procs={1001: first, 1002: second})
        spawned = _install_stubs(monkeypatch, backend, [first, second])
        mgr = _make_manager(tmp_path)

        pid, log_file = await mgr.start_process(
            command="sleep 30",
            working_dir="/workspace",
            container_id="cid",
            exec_backend=_exec_backend(),
        )

        assert pid == 1002
        assert spawned == [first, second]
        # 宿主级杀树：只杀 attempt1 的宿主客户端，绝不经后端传输通道杀
        assert backend.host_killed == [1001]
        assert backend.kill_calls == []
        assert backend.probe_calls == 1
        # attempt1 日志留痕：超时 + 宿主级杀树
        first_log = tmp_path / "logs" / "bash_1001.log"
        assert first_log.exists()
        first_text = first_log.read_text(encoding="utf-8")
        assert "timed out" in first_text
        assert "Host client tree killed" in first_text
        # attempt2 日志留痕：重试来源可追溯
        text = log_file.read_text(encoding="utf-8")
        assert "previous attempt 1/2" in text
        assert "host_pid=1001" in text

        await mgr.shutdown_all(force=True)

    async def test_no_retry_after_pid_line_received(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """收到首行 = 命令已启动：即使立刻非零退出也不探测、不重试。"""
        proc = _StubProcess(3001)
        proc.emit(b"777\n")
        proc.die(1)
        backend = _FakeWslBackend(probe=True, procs={3001: proc})
        spawned = _install_stubs(monkeypatch, backend, [proc])
        mgr = _make_manager(tmp_path)

        pid, _ = await mgr.start_process(
            command="false",
            working_dir="/workspace",
            container_id="cid",
            exec_backend=_exec_backend(),
        )

        assert pid == 3001
        assert spawned == [proc]
        assert backend.probe_calls == 0
        assert backend.host_killed == []
        assert backend.kill_calls == []

        await mgr.shutdown_all(force=True)


# ─────────────────── 契约 3：探测失败 → 不重试 + 熔断冷却 ───────────────────


class TestProbeUnhealthyFailFast:
    async def test_probe_unhealthy_fails_fast_without_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _StubProcess(2001)
        backend = _FakeWslBackend(probe=False, procs={2001: first})
        spawned = _install_stubs(monkeypatch, backend, [first])
        mgr = _make_manager(tmp_path)

        with pytest.raises(BackendUnhealthyError):
            await mgr.start_process(
                command="sleep 30",
                working_dir="/workspace",
                container_id="cid",
                exec_backend=_exec_backend(),
            )

        assert spawned == [first]
        assert backend.host_killed == [2001]
        assert backend.kill_calls == []
        assert backend.probe_calls == 1

    async def test_circuit_breaker_blocks_subsequent_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _StubProcess(2101)
        backend = _FakeWslBackend(probe=False, procs={2101: first})
        spawned = _install_stubs(monkeypatch, backend, [first])
        mgr = _make_manager(tmp_path)

        with pytest.raises(BackendUnhealthyError):
            await mgr.start_process(
                command="sleep 30",
                working_dir="/workspace",
                container_id="cid",
                exec_backend=_exec_backend(),
            )
        assert len(spawned) == 1

        # 冷却期内：不再 spawn，直接快速失败
        with pytest.raises(BackendUnhealthyError):
            await mgr.start_process(
                command="sleep 30",
                working_dir="/workspace",
                container_id="cid",
                exec_backend=_exec_backend(),
            )
        assert len(spawned) == 1

    async def test_last_attempt_timeout_raises_spawn_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重试次数用尽仍无首行 → SpawnFirstLineTimeoutError（携带宿主 pid/日志）。"""
        first = _StubProcess(2201)
        second = _StubProcess(2202)
        backend = _FakeWslBackend(probe=True, procs={2201: first, 2202: second})
        _install_stubs(monkeypatch, backend, [first, second])
        mgr = _make_manager(tmp_path)

        with pytest.raises(SpawnFirstLineTimeoutError) as exc_info:
            await mgr.start_process(
                command="sleep 30",
                working_dir="/workspace",
                container_id="cid",
                exec_backend=_exec_backend(),
            )

        assert exc_info.value.attempt == 2
        assert exc_info.value.max_attempts == 2
        assert exc_info.value.host_pid == 2202
        assert backend.host_killed == [2201, 2202]
        assert backend.kill_calls == []


# ─────────────────── 契约 4：首行读取不再退回宿主 pid ───────────────────


class TestFirstLineProtocol:
    async def test_read_container_pid_returns_none_on_timeout(self) -> None:
        """读不到首行必须返回 None —— 绝不把宿主 pid 伪装成容器内 pid。"""
        proc = _StubProcess(4001)
        result = await ProcessManager._read_container_pid(proc, timeout=0.05)
        assert result is None
