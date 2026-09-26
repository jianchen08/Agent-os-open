# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""bridge.py 分支覆盖补充测试（Node runtime 子进程桥的生命周期与协议容错）。

打桩约定：
- Node 子进程是外部依赖 → SpawnStub/FakeProc/FakeStdin + asyncio.StreamReader
  替身，唯一替换点是 asyncio.create_subprocess_exec 这一处外部边界；
  _read_loop / _drain_stderr / 帧解析 / 等待者唤醒全部走真实代码路径；
- 流式喂帧（feed_data/feed_eof）模拟 runtime 的正常响应、脏帧、EOF 与管道爆炸；
- 等待时序用真实小步轮询（_wait_until），不做零延迟假定时钟；
- 不可杀进程分支的真实 5s 兜底等待以 wait_for 定点收敛（纯时钟控制，
  不参与行为断言），避免单测拖慢 5 秒。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_BRIDGE_MODULE = "dsh_adapter_bridge_gaps_test"

_INIT_TOOLS = [{"name": "echo", "description": "dsh echo tool"}]


def _load_bridge_module() -> Any:
    """显式文件级加载（唯一模块名）：防裸 `import bridge` 被其它插件目录劫持。"""
    spec = importlib.util.spec_from_file_location(_BRIDGE_MODULE, PLUGIN_DIR / "bridge.py")
    assert spec is not None and spec.loader is not None, "cannot load dsh_adapter bridge.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_BRIDGE_MODULE] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bmod() -> Any:
    return _load_bridge_module()


def _response_frame(req_id: int, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _wire_line(frame: dict[str, Any]) -> bytes:
    return json.dumps(frame).encode("utf-8") + b"\n"


async def _wait_until(pred: Callable[[], bool], timeout_s: float = 2.0) -> None:
    """真实小步轮询等待异步写入面就绪（不做零延迟假定时钟）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.005)


class FakeStdin:
    """子进程 stdin 替身：按 newline 协议收集线上帧（外部边界可观察面）。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._buf = b""

    def write(self, data: bytes) -> int:
        self._buf += data
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            self.frames.append(json.loads(line.decode("utf-8")))
        return len(data)

    async def drain(self) -> None:
        return None


class BrokenAfterFirstWrite(FakeStdin):
    """boot 帧成功上线后管道即断：模拟 runtime 收到帧后死亡（真实 EPIPE 形态）。"""

    def write(self, data: bytes) -> int:
        if self.frames:
            raise BrokenPipeError("broken pipe to dead process")
        return super().write(data)


class FakeProc:
    """Node 子进程替身：kill/wait 均记录于自身（外部边界可观察面）。"""

    def __init__(self, stdin: Any, stdout: Any, stderr: Any, kill_works: bool = True) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self.kill_count = 0
        self._kill_works = kill_works

    def kill(self) -> None:
        self.kill_count += 1
        if self._kill_works:
            self.returncode = 0

    def wait(self) -> asyncio.Future[int]:
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        if self.returncode is not None:
            fut.set_result(self.returncode)
        return fut


class _ExplodingStream:
    """一读即炸的流：模拟底层管道故障（区别于正常 EOF）。"""

    def __aiter__(self) -> _ExplodingStream:
        return self

    async def __anext__(self) -> bytes:
        raise RuntimeError("pipe exploded")

    async def readline(self) -> bytes:
        raise RuntimeError("pipe exploded")


class SpawnStub:
    """asyncio.create_subprocess_exec 替身：记录调用参数，交付预置子进程。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._proc: FakeProc | None = None

    def make(
        self, stdin: Any = None, stdout: Any = None, stderr: Any = None, kill_works: bool = True
    ) -> FakeProc:
        self._proc = FakeProc(stdin, stdout, stderr, kill_works)
        return self._proc

    async def __call__(self, program: str, *args: str, **kwargs: Any) -> FakeProc:
        self.calls.append({"program": program, "args": args, **kwargs})
        assert self._proc is not None, "test must preconfigure the child via make()"
        return self._proc


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> SpawnStub:
    stub = SpawnStub()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", stub)
    return stub


# ── 启动前置校验：repo root 缺失 / 运行中仓库消失 ─────────────────────


class TestBootUnavailable:
    def test_repo_root_not_a_dir_fails_boot_with_env_hint(
        self, bmod: Any, tmp_path: Path
    ) -> None:
        gone = tmp_path / "no-such-repo"
        bridge = bmod.DshRuntimeBridge(repo_root=str(gone))
        out = asyncio.run(bridge.call_tool("read", {"file_path": "a.txt"}))
        assert out["success"] is False
        assert out["data"] is None and out["duration_ms"] == 0.0
        # 错误可定位可恢复：含缺失路径与所需环境变量名
        assert "boot failed" in out["error"]
        assert str(gone) in out["error"]
        assert "AGENTOS_DSH_REPO_ROOT" in out["error"]

    def test_repo_removed_after_boot_denies_subsequent_calls(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        async def scenario() -> dict[str, Any]:
            reader = asyncio.StreamReader()
            proc = spawn.make(stdin=FakeStdin(), stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(repo))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            assert await bridge.initialize() == _INIT_TOOLS
            proc.returncode = 1  # 子进程死亡（外部状态），下次调用应走重启校验
            shutil.rmtree(repo)  # 随后仓库根也消失 → 重启前置校验失败
            return await bridge.call_tool("read", {"file_path": "a.txt"})

        out = asyncio.run(scenario())
        assert out["success"] is False
        assert "not found" in out["error"]
        assert str(repo) in out["error"]
        assert "boot failed" not in out["error"]  # post-boot deny 不带 boot 封装前缀


# ── spawn 装配面：env 携带 repo root 与可选外包装载区 ──────────────────


class TestSpawnEnv:
    @pytest.mark.parametrize("extra", [None, "Z:/custom/extra-plugins"])
    def test_spawn_env_carries_repo_root_and_optional_extra(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub, extra: str | None
    ) -> None:
        async def scenario() -> list[dict[str, Any]]:
            reader = asyncio.StreamReader()
            spawn.make(stdin=FakeStdin(), stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path), extra_plugins_dir=extra)
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            return await bridge.list_tools()

        tools = asyncio.run(scenario())
        assert tools == _INIT_TOOLS
        call = spawn.calls[0]
        assert call["program"] == "node"
        assert call["args"][-1].endswith("dsh-rpc-bridge.mjs")
        assert call["env"]["AGENTOS_DSH_REPO_ROOT"] == str(tmp_path)
        if extra is None:
            assert "AGENTOS_DSH_EXTRA_PLUGINS_DIR" not in call["env"]
        else:
            assert call["env"]["AGENTOS_DSH_EXTRA_PLUGINS_DIR"] == extra


# ── stderr 排空：空 stderr 与日志转发 ─────────────────────────────────


class TestStderrDrain:
    def test_null_stderr_process_boots_and_calls(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        tool_result = {
            "success": True,
            "data": {"tool": "read"},
            "error": None,
            "duration_ms": 1.0,
        }

        async def scenario() -> dict[str, Any]:
            out_reader = asyncio.StreamReader()
            stdin = FakeStdin()
            proc = spawn.make(stdin=stdin, stdout=out_reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            out_reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            call_task = asyncio.create_task(bridge.call_tool("read", {"file_path": "a.txt"}))
            # 应答帧懒喂：请求帧上线后再喂对应应答（预喂会被读循环丢弃）
            await _wait_until(lambda: len(stdin.frames) == 2)  # initialize + tool/call
            out_reader.feed_data(_wire_line(_response_frame(2, tool_result)))
            out = await asyncio.wait_for(call_task, 2)
            shtask = asyncio.create_task(bridge.shutdown())
            await _wait_until(lambda: len(stdin.frames) == 3)
            out_reader.feed_data(_wire_line(_response_frame(3, None)))  # shutdown 应答
            out_reader.feed_eof()
            await asyncio.wait_for(shtask, 2)
            assert proc.kill_count == 1
            return out

        assert asyncio.run(scenario()) == tool_result  # 信封原样透传

    def test_stderr_lines_drained_to_debug_log(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def scenario() -> list[dict[str, Any]]:
            err = asyncio.StreamReader()
            for line in (b"[dsh] booting\n", b"   \n", b"[dsh] ready\n"):
                err.feed_data(line)
            err.feed_eof()
            out_reader = asyncio.StreamReader()
            proc = spawn.make(stdin=FakeStdin(), stdout=out_reader, stderr=err)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            out_reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            out_reader.feed_eof()
            tools = await bridge.initialize()
            while not err.at_eof():  # 等排空任务消费完缓冲（真实小步轮询）
                await asyncio.sleep(0.005)
            proc.returncode = 1  # 排空已收尾：shutdown 走收尸路径
            await bridge.shutdown()
            return tools

        with caplog.at_level(logging.DEBUG):
            tools = asyncio.run(scenario())
        assert tools == _INIT_TOOLS
        drained = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG and r.getMessage().startswith("[dsh-bridge]")
        ]
        assert any("[dsh] booting" in m for m in drained)
        assert any("[dsh] ready" in m for m in drained)
        assert len(drained) == 2  # 空白行不转发


# ── stdout 读循环：空 stdout / 空行 / 脏帧 / EOF 唤醒等待者 ────────────


class TestReadLoopFrames:
    def test_null_stdout_boot_times_out_fail_visible(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> dict[str, Any]:
            proc = spawn.make(stdin=FakeStdin(), stdout=None, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path), boot_timeout_s=0.05)
            out = await bridge.call_tool("read", {"file_path": "a.txt"})
            proc.returncode = 1  # 读循环已死（stdout 缺失）：shutdown 走收尸路径
            await bridge.shutdown()
            return out

        out = asyncio.run(scenario())
        assert out["success"] is False
        assert "boot failed" in out["error"]

    @pytest.mark.parametrize(
        "junk", [b"not-json{{{", b'{"jsonrpc": 2.0,}', b"\x00\x01\x02binary"]
    )
    def test_blank_and_junk_frames_skipped_until_good_frame(
        self,
        bmod: Any,
        tmp_path: Path,
        spawn: SpawnStub,
        caplog: pytest.LogCaptureFixture,
        junk: bytes,
    ) -> None:
        async def scenario() -> list[dict[str, Any]]:
            reader = asyncio.StreamReader()
            spawn.make(stdin=FakeStdin(), stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            reader.feed_data(b"\n")
            reader.feed_data(b"   \n")
            reader.feed_data(junk + b"\n")
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            return await bridge.initialize()

        with caplog.at_level(logging.WARNING):
            tools = asyncio.run(scenario())
        assert tools == _INIT_TOOLS  # 脏帧不阻断后续正常帧
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # 空白行不告警，仅脏帧告警一次
        assert "bad frame" in warnings[0].getMessage()
        assert junk.decode("utf-8", errors="replace") in warnings[0].getMessage()

    def test_boot_stdout_eof_fails_pending_boot(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> tuple[dict[str, Any], FakeStdin]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            proc = spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path), boot_timeout_s=5)
            task = asyncio.create_task(bridge.call_tool("read", {"file_path": "a.txt"}))
            await _wait_until(lambda: len(stdin.frames) == 1)
            reader.feed_eof()  # runtime 静默退出
            out = await asyncio.wait_for(task, 2)
            proc.returncode = 1  # 读循环已死（EOF）：shutdown 走收尸路径
            await bridge.shutdown()
            return out, stdin

        out, stdin = asyncio.run(scenario())
        assert stdin.frames[0]["method"] == "initialize"
        assert out["success"] is False
        assert "process exited" in out["error"]
        assert "boot failed" in out["error"]

    def test_post_boot_stdout_eof_denies_tool_call(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> dict[str, Any]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            assert await bridge.initialize() == _INIT_TOOLS
            task = asyncio.create_task(bridge.call_tool("read", {"file_path": "a.txt"}))
            await _wait_until(lambda: len(stdin.frames) == 2)
            reader.feed_eof()
            return await asyncio.wait_for(task, 2)

        out = asyncio.run(scenario())
        assert out["success"] is False
        assert "process exited" in out["error"]
        assert "boot failed" not in out["error"]


# ── teardown 韧性：管道爆炸留痕、不可杀进程兜底 ────────────────────────


class TestTeardownResilience:
    def test_exploding_pipes_logged_not_raised(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def scenario() -> dict[str, Any]:
            proc = spawn.make(
                stdin=FakeStdin(), stdout=_ExplodingStream(), stderr=_ExplodingStream()
            )
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path), boot_timeout_s=0.05)
            out = await bridge.call_tool("read", {"file_path": "a.txt"})
            proc.returncode = 1  # 管道已爆：shutdown 走收尸路径
            await bridge.shutdown()
            await asyncio.sleep(0)  # 冲刷 done 回调
            return out

        with caplog.at_level(logging.ERROR):
            out = asyncio.run(scenario())
        assert out["success"] is False  # 管道爆炸不致崩溃，仅 boot 失败
        msgs = [r.getMessage() for r in caplog.records]
        assert any("reader task raised during teardown" in m for m in msgs)
        assert any("stderr task raised during teardown" in m for m in msgs)
        assert any(
            "drain_stderr" in m and "pipe exploded" in m for m in msgs
        )  # 后台任务异常经回调留痕，不静默丢失

    def test_unkillable_process_teardown_times_out_and_survives(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_wait_for = asyncio.wait_for

        async def fast_timeout_wait_for(fut: Any, timeout: float | None = None) -> Any:
            if timeout == 5:  # teardown 的 5s 兜底收敛为即时（时钟控制，仅驱动防御分支）
                raise TimeoutError()
            return await real_wait_for(fut, timeout)

        monkeypatch.setattr(asyncio, "wait_for", fast_timeout_wait_for)

        async def scenario() -> tuple[list[dict[str, Any]], FakeProc]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            proc = spawn.make(stdin=stdin, stdout=reader, stderr=None, kill_works=False)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            tools = await bridge.initialize()
            shtask = asyncio.create_task(bridge.shutdown())
            await _wait_until(lambda: len(stdin.frames) == 2)
            reader.feed_data(_wire_line(_response_frame(2, None)))  # shutdown 应答
            reader.feed_eof()
            await asyncio.wait_for(shtask, 2)
            return tools, proc

        tools, proc = asyncio.run(scenario())
        assert tools == _INIT_TOOLS
        assert proc.kill_count == 1  # kill 已尝试，超时不阻断收尾


# ── shutdown：优雅帧 + 管道断裂兜底 ───────────────────────────────────


class TestShutdown:
    def test_graceful_shutdown_sends_rpc_then_kills(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> tuple[FakeStdin, FakeProc]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            proc = spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            assert await bridge.initialize() == _INIT_TOOLS  # 先启动子进程
            shtask = asyncio.create_task(bridge.shutdown())
            await _wait_until(lambda: len(stdin.frames) == 2)
            reader.feed_data(_wire_line(_response_frame(2, None)))
            reader.feed_eof()
            await asyncio.wait_for(shtask, 2)
            return stdin, proc

        stdin, proc = asyncio.run(scenario())
        assert [f["method"] for f in stdin.frames] == ["initialize", "shutdown"]
        assert proc.kill_count == 1

    def test_shutdown_survives_broken_pipe_on_shutdown_rpc(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> tuple[FakeStdin, FakeProc]:
            reader = asyncio.StreamReader()
            stdin = BrokenAfterFirstWrite()
            proc = spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            assert await bridge.initialize() == _INIT_TOOLS  # boot 帧成功上线
            await bridge.shutdown()  # shutdown 帧写即断管：兜底吞掉，仍清壳
            return stdin, proc

        stdin, proc = asyncio.run(scenario())
        assert [f["method"] for f in stdin.frames] == ["initialize"]  # shutdown 帧未上线
        assert proc.kill_count == 1


# ── list_tools：缓存命中不走线上，未缓存走 initialize ──────────────────


class TestListTools:
    def test_uncached_boots_over_initialize_rpc(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> tuple[list[dict[str, Any]], FakeStdin]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            return await bridge.list_tools(), stdin

        tools, stdin = asyncio.run(scenario())
        assert tools == _INIT_TOOLS
        assert [f["method"] for f in stdin.frames] == ["initialize"]

    def test_cached_returns_without_new_rpc(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> tuple[list[dict[str, Any]], FakeStdin]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(repo_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            assert await bridge.initialize() == _INIT_TOOLS
            assert len(stdin.frames) == 1
            cached = await bridge.list_tools()
            assert len(stdin.frames) == 1  # 缓存命中：零新增线上帧
            return cached, stdin

        cached, stdin = asyncio.run(scenario())
        assert cached == _INIT_TOOLS
        assert [f["method"] for f in stdin.frames] == ["initialize"]


# ── 模块级单例：get_bridge / shutdown_bridge ──────────────────────────


class TestModuleSingleton:
    def test_get_bridge_memoizes_single_instance(self, bmod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(bmod, "_bridge", None)
        first = bmod.get_bridge()
        second = bmod.get_bridge()
        assert isinstance(first, bmod.DshRuntimeBridge)
        assert first is second

    def test_first_create_carries_extra_plugins_dir_to_runtime_env(
        self, bmod: Any, monkeypatch: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        monkeypatch.setenv("AGENTOS_DSH_REPO_ROOT", str(tmp_path))
        monkeypatch.setattr(bmod, "_bridge", None)
        bridge = bmod.get_bridge(extra_plugins_dir="Z:/custom/extra")

        async def scenario() -> list[dict[str, Any]]:
            reader = asyncio.StreamReader()
            spawn.make(stdin=FakeStdin(), stdout=reader, stderr=None)
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            return await bridge.list_tools()

        assert asyncio.run(scenario()) == _INIT_TOOLS
        env = spawn.calls[0]["env"]
        assert env["AGENTOS_DSH_REPO_ROOT"] == str(tmp_path)
        assert env["AGENTOS_DSH_EXTRA_PLUGINS_DIR"] == "Z:/custom/extra"

    def test_shutdown_bridge_noop_when_absent(self, bmod: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(bmod, "_bridge", None)
        asyncio.run(bmod.shutdown_bridge())  # 无桥可关：不抛
        first = bmod.get_bridge()
        second = bmod.get_bridge()
        assert first is second  # 收尾后单例语义不变

    def test_shutdown_bridge_awaits_bridge_shutdown_and_resets(
        self, bmod: Any, monkeypatch: Any
    ) -> None:
        stopped: list[bool] = []

        class _Stub:
            async def shutdown(self) -> None:
                stopped.append(True)

        monkeypatch.setattr(bmod, "_bridge", _Stub())
        asyncio.run(bmod.shutdown_bridge())
        assert stopped == [True]
        monkeypatch.setattr(bmod, "_bridge", None)
        assert isinstance(bmod.get_bridge(), bmod.DshRuntimeBridge)  # 复位后惰性重建
