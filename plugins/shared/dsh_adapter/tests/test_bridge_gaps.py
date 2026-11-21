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
if str(PLUGIN_DIR) not in sys.path:
    # bridge.get_bridge 缺省 boot 计划走 runtime_workspace（插件进程内裸名同名
    # 约定；本文件以文件级 spec 加载 bridge，需补齐插件目录到 sys.path）
    sys.path.insert(0, str(PLUGIN_DIR))
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
        bridge = bmod.DshRuntimeBridge(runtime_root=str(gone))
        out = asyncio.run(bridge.call_tool("read", {"file_path": "a.txt"}))
        assert out["success"] is False
        assert out["data"] is None
        assert out["duration_ms"] == 0
        assert isinstance(out["duration_ms"], int), "duration_ms 必须整数毫秒（内核 u64 严格反序列化）"
        # 错误可定位可恢复：含缺失路径与所需环境变量名
        assert "boot failed" in out["error"]
        assert str(gone) in out["error"]
        assert "AGENTOS_DSH_RUNTIME_ROOT" in out["error"]

    def test_repo_removed_after_boot_denies_subsequent_calls(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        async def scenario() -> dict[str, Any]:
            reader = asyncio.StreamReader()
            proc = spawn.make(stdin=FakeStdin(), stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(runtime_root=str(repo))
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


# ── spawn 装配面：env 携带工作区根 + boot 裁剪轴随 initialize 下发 ──────────


class TestSpawnEnv:
    def test_spawned_process_env_carries_runtime_root(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> list[dict[str, Any]]:
            reader = asyncio.StreamReader()
            spawn.make(stdin=FakeStdin(), stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            return await bridge.list_tools()

        tools = asyncio.run(scenario())
        assert tools == _INIT_TOOLS
        call = spawn.calls[0]
        assert call["program"] == "node"
        assert call["args"][-1].endswith("dsh-rpc-bridge.mjs")
        assert call["env"]["AGENTOS_DSH_RUNTIME_ROOT"] == str(tmp_path)


class TestBootPlanWire:
    """boot 裁剪轴（AC-R4）随 initialize 帧下发；显式传参覆盖构造期缺省。"""

    def test_initialize_frame_carries_default_boot_plan(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> tuple[list[dict[str, Any]], FakeStdin]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            tools = await bridge.list_tools()
            return tools, stdin

        tools, stdin = asyncio.run(scenario())
        assert tools == _INIT_TOOLS
        params = stdin.frames[0]["params"]
        assert params["bootPlan"] == {
            "source": "profile",
            "fallback": {"chain": True, "packages": []},
        }  # 协议级缺省=profile composition + 最小链回退

    def test_explicit_boot_plan_overrides_constructor_default(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        empty = {"chain": False, "packages": []}

        async def scenario() -> tuple[list[dict[str, Any]], FakeStdin]:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {"tools": []})))
            tools = await bridge.initialize(boot_plan=empty)  # 空工作区 plan：不加载任何 DSH 包
            return tools, stdin

        tools, stdin = asyncio.run(scenario())
        assert tools == []
        assert stdin.frames[0]["params"]["bootPlan"]["chain"] is False


# ── stderr 排空：空 stderr 与日志转发 ─────────────────────────────────


class TestStderrDrain:
    def test_null_stderr_process_boots_and_calls(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        tool_result = {
            "success": True,
            "data": {"tool": "read"},
            "error": None,
            "duration_ms": 1,
        }

        async def scenario() -> dict[str, Any]:
            out_reader = asyncio.StreamReader()
            stdin = FakeStdin()
            proc = spawn.make(stdin=stdin, stdout=out_reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path), boot_timeout_s=0.05)
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path), boot_timeout_s=5)
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path), boot_timeout_s=0.05)
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
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

    def test_get_bridge_injects_manager_config_into_existing_singleton(
        self, bmod: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(bmod, "_bridge", None)
        first = bmod.get_bridge()
        again = bmod.get_bridge(manager_config={"registry": "https://r.example/"})
        assert again is first
        assert first._manager_config == {"registry": "https://r.example/"}  # noqa: SLF001 — 单例注入面断言

    def test_first_create_carries_runtime_root(
        self, bmod: Any, monkeypatch: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        monkeypatch.setenv("AGENTOS_DSH_RUNTIME_ROOT", str(tmp_path))
        monkeypatch.setattr(bmod, "_bridge", None)
        bridge = bmod.get_bridge()

        async def scenario() -> list[dict[str, Any]]:
            reader = asyncio.StreamReader()
            spawn.make(stdin=FakeStdin(), stdout=reader, stderr=None)
            reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS})))
            reader.feed_eof()
            return await bridge.list_tools()

        assert asyncio.run(scenario()) == _INIT_TOOLS
        env = spawn.calls[0]["env"]
        assert env["AGENTOS_DSH_RUNTIME_ROOT"] == str(tmp_path)

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


# ── E1 桥扩展方法（AC-X1 service/call、AC-X3 hook/emit、AC-X2 http/request）──


class TestExtendedProtocol:
    """扩展方法线格式与信封。帧序纪律：响应帧必须在对应 `_pending` 注册后
    才可喂入（读循环对未知 id 的帧按协议丢弃）——先 initialize 再喂目标帧。"""

    def _boot(self, bmod: Any, tmp_path: Path, spawn: SpawnStub,
              server_info: dict[str, Any] | None = None) -> tuple[Any, FakeStdin, asyncio.StreamReader]:
        reader = asyncio.StreamReader()
        stdin = FakeStdin()
        spawn.make(stdin=stdin, stdout=reader, stderr=None)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        reader.feed_data(_wire_line(_response_frame(
            1, {"tools": _INIT_TOOLS, "serverInfo": server_info or {}})))
        return bridge, stdin, reader

    def test_call_service_wire_and_envelope(self, bmod: Any, tmp_path: Path,
                                            spawn: SpawnStub) -> None:
        async def scenario() -> tuple[dict[str, Any], FakeStdin]:
            bridge, stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            reader.feed_data(_wire_line(_response_frame(
                2, {"success": True, "data": {"pong": 1}, "error": None, "duration_ms": 2})))
            out = await bridge.call_service("svc", "ping", [1, 2])
            return out, stdin

        out, stdin = asyncio.run(scenario())
        assert out == {"success": True, "data": {"pong": 1}, "error": None, "duration_ms": 2}
        frame = stdin.frames[1]
        assert frame["method"] == "service/call"
        assert frame["params"] == {"name": "svc", "method": "ping", "args": [1, 2],
                                   "timeoutMs": 120000}

    def test_call_service_boot_failure_returns_envelope(self, bmod: Any, tmp_path: Path) -> None:
        gone = tmp_path / "gone"
        bridge = bmod.DshRuntimeBridge(runtime_root=str(gone))
        out = asyncio.run(bridge.call_service("svc", "ping"))
        assert out["success"] is False
        assert out["duration_ms"] == 0
        assert "boot failed" in out["error"]

    def test_emit_hook_and_web_origin(self, bmod: Any, tmp_path: Path,
                                      spawn: SpawnStub) -> None:
        async def scenario() -> tuple[dict[str, Any], FakeStdin, Any]:
            bridge, stdin, reader = self._boot(
                bmod, tmp_path, spawn, {"webServerOrigin": "http://127.0.0.1:3456"})
            await bridge.initialize()
            reader.feed_data(_wire_line(_response_frame(
                2, {"ok": True, "event": "turn/end", "decision": {"decision": "deny"}, "error": None})))
            out = await bridge.emit_hook("turn/end", {"reason": "x"})
            return out, stdin, bridge

        out, stdin, bridge = asyncio.run(scenario())
        assert out["ok"] is True
        assert out["decision"] == {"decision": "deny"}
        assert bridge.web_server_origin == "http://127.0.0.1:3456"
        frame = stdin.frames[1]
        assert frame["method"] == "hook/emit"
        assert frame["params"] == {"event": "turn/end", "payload": {"reason": "x"}}

    def test_emit_hook_bridge_error_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                                    spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            reader.feed_data(_wire_line({
                "jsonrpc": "2.0", "id": 2,
                "error": {"code": -32000, "message": "boom"},
            }))
            return await bridge.emit_hook("turn/end")

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "boom" in out["error"]

    def test_emit_hook_non_object_result_fail_visible(self, bmod: Any, tmp_path: Path,
                                                     spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            reader.feed_data(_wire_line(_response_frame(2, ["not", "an", "object"])))
            return await bridge.emit_hook("turn/end")

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "non-object" in out["error"]

    def test_http_request_wire(self, bmod: Any, tmp_path: Path, spawn: SpawnStub) -> None:
        async def scenario() -> tuple[dict[str, Any], FakeStdin]:
            bridge, stdin, reader = self._boot(
                bmod, tmp_path, spawn, {"webServerOrigin": "http://127.0.0.1:3456"})
            await bridge.initialize()
            reader.feed_data(_wire_line(_response_frame(2, {
                "ok": True, "status": 200, "headers": {"content-type": "text/plain"},
                "body_base64": "aGk=", "error": None, "duration_ms": 1,
            })))
            out = await bridge.http_request("POST", "/api/x", {"accept": "*/*"}, b"hi")
            return out, stdin

        out, stdin = asyncio.run(scenario())
        assert out["ok"] is True
        assert out["status"] == 200
        frame = stdin.frames[1]
        assert frame["method"] == "http/request"
        assert frame["params"]["path"] == "/api/x"
        assert frame["params"]["body_base64"] == "aGk="

    def test_http_request_reports_missing_web_server(self, bmod: Any, tmp_path: Path,
                                                     spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, _reader = self._boot(
                bmod, tmp_path, spawn,
                {"webServerOrigin": None,
                 "webServerError": "webServer 轴需要 @deepseek-ai/dsh-host-webserver"})
            await bridge.initialize()
            return await bridge.http_request("GET", "/x")

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "dsh-host-webserver" in out["error"]

    def test_http_request_non_object_result_fail_visible(self, bmod: Any, tmp_path: Path,
                                                         spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, reader = self._boot(
                bmod, tmp_path, spawn, {"webServerOrigin": "http://127.0.0.1:3456"})
            await bridge.initialize()
            reader.feed_data(_wire_line(_response_frame(2, "not-an-object")))
            return await bridge.http_request("GET", "/x")

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "non-object" in out["error"]


# ── 官方 plugin-manager RPC（2026-11-09）────────────────────────────


class TestManagerRPC:
    """manager_* 方法线格式与 ``{ok, data, error}`` 信封。"""

    def _boot(self, bmod: Any, tmp_path: Path, spawn: SpawnStub,
              server_info: dict[str, Any] | None = None,
              manager: dict[str, Any] | None = None) -> tuple[Any, FakeStdin, asyncio.StreamReader]:
        reader = asyncio.StreamReader()
        stdin = FakeStdin()
        spawn.make(stdin=stdin, stdout=reader, stderr=None)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path), manager_config=manager)
        reader.feed_data(_wire_line(_response_frame(
            1, {"tools": _INIT_TOOLS, "serverInfo": server_info or {}})))
        return bridge, stdin, reader

    def test_initialize_carries_manager_config(self, bmod: Any, tmp_path: Path,
                                               spawn: SpawnStub) -> None:
        async def scenario() -> FakeStdin:
            bridge, stdin, _reader = self._boot(
                bmod, tmp_path, spawn, manager={"pnpmCommand": "pnpm", "registry": "https://r"})
            await bridge.initialize()
            return stdin

        stdin = asyncio.run(scenario())
        assert stdin.frames[0]["params"]["manager"] == {
            "pnpmCommand": "pnpm", "registry": "https://r"}

    def test_manager_ready_and_error_from_server_info(self, bmod: Any, tmp_path: Path,
                                                      spawn: SpawnStub) -> None:
        async def scenario() -> Any:
            bridge, _stdin, _reader = self._boot(
                bmod, tmp_path, spawn,
                {"managerReady": True, "managerError": None})
            await bridge.initialize()
            return bridge

        bridge = asyncio.run(scenario())
        assert bridge.manager_ready is True
        assert bridge.manager_error is None

    def test_manager_list_wire_and_envelope(self, bmod: Any, tmp_path: Path,
                                            spawn: SpawnStub) -> None:
        async def scenario() -> tuple[dict[str, Any], FakeStdin]:
            bridge, stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            payload = {"ok": True, "data": {"bundles": [], "plugins": [], "registries": None},
                       "error": None}
            reader.feed_data(_wire_line(_response_frame(2, payload)))
            return await bridge.manager_list(), stdin

        out, stdin = asyncio.run(scenario())
        assert out["ok"] is True
        assert out["data"]["bundles"] == []
        assert stdin.frames[1]["method"] == "plugin/manager/list"

    def test_manager_inspect_install_cancel_remove_enable_registries_wire(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        async def scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            bridge, stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            results = []
            calls = [
                (bridge.manager_inspect("spec-a"), {"ok": True, "data": {"status": "accepted"}}),
                (bridge.manager_install("spec-a", "req-1", {"saveExact": True}),
                 {"ok": True, "data": {"application": "restart-required"}}),
                (bridge.manager_cancel("req-1"), {"ok": True, "data": {"status": "cancelled"}}),
                (bridge.manager_remove("bundle-a"), {"ok": True, "data": {"application": "restart-required"}}),
                (bridge.manager_set_enabled("bundle-a", True), {"ok": True, "data": {"changed": True}}),
                (bridge.manager_set_enabled("bundle-a", False), {"ok": True, "data": {"changed": True}}),
                (bridge.manager_registries(), {"ok": True, "data": {"registry": None}}),
            ]
            for idx, (coro, payload) in enumerate(calls, start=2):
                reader.feed_data(_wire_line(_response_frame(idx, payload)))
                results.append(await coro)
            return results, stdin.frames

        results, frames = asyncio.run(scenario())
        methods = [frame["method"] for frame in frames[1:]]
        assert methods == [
            "plugin/manager/inspect", "plugin/manager/install", "plugin/manager/cancel",
            "plugin/manager/remove", "plugin/manager/enable", "plugin/manager/disable",
            "plugin/manager/registries",
        ]
        assert frames[2]["params"] == {"spec": "spec-a", "requestId": "req-1",
                                        "options": {"saveExact": True}}
        assert results[0]["data"]["status"] == "accepted"
        assert results[1]["data"]["application"] == "restart-required"

    def test_manager_bridge_error_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                                  spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            reader.feed_data(_wire_line({
                "jsonrpc": "2.0", "id": 2,
                "error": {"code": -32000, "message": "boom"},
            }))
            return await bridge.manager_list()

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "boom" in out["error"]

    def test_manager_non_object_result_fail_visible(self, bmod: Any, tmp_path: Path,
                                                   spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            reader.feed_data(_wire_line(_response_frame(2, ["not", "object"])))
            return await bridge.manager_list()

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "non-object" in out["error"]

    def test_manager_timeout_fail_visible(self, bmod: Any, tmp_path: Path,
                                          spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, _reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            return await bridge.manager_call("list", {}, timeout_s=0.01)

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "Timeout" in out["error"] or "timeout" in out["error"].lower()

    def test_manager_boot_failure_fail_visible(self, bmod: Any, tmp_path: Path) -> None:
        gone = tmp_path / "gone"
        bridge = bmod.DshRuntimeBridge(runtime_root=str(gone))
        out = asyncio.run(bridge.manager_list())
        assert out["ok"] is False
        assert "boot failed" in out["error"]

    def test_node_missing_maps_to_bridge_unavailable(self, bmod: Any, tmp_path: Path,
                                                     monkeypatch: Any) -> None:
        async def missing(*_a: Any, **_k: Any) -> Any:
            raise FileNotFoundError("node")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(bridge.manager_list())
        assert out["ok"] is False
        assert "node_missing" in out["error"]

    def test_restart_bridge_shuts_down_and_resets(self, bmod: Any, monkeypatch: Any) -> None:
        stopped: list[bool] = []

        class _Stub:
            async def shutdown(self) -> None:
                stopped.append(True)

        monkeypatch.setattr(bmod, "_bridge", _Stub())
        asyncio.run(bmod.restart_bridge())
        assert stopped == [True]
        assert bmod._bridge is None


# ── 宿主依赖补装 RPC（2026-11-09 翻译面接通）────────────────────────


class TestHostDepRPC:
    """``manager_add_host_deps``：空清单短路 + 线格式 + 失败信封。"""

    def _boot(self, bmod: Any, tmp_path: Path,
              spawn: SpawnStub) -> tuple[Any, FakeStdin, asyncio.StreamReader]:
        reader = asyncio.StreamReader()
        stdin = FakeStdin()
        spawn.make(stdin=stdin, stdout=reader, stderr=None)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS, "serverInfo": {}})))
        return bridge, stdin, reader

    def test_empty_specs_short_circuit_without_rpc(self, bmod: Any, tmp_path: Path,
                                                   spawn: SpawnStub) -> None:
        async def scenario() -> tuple[dict[str, Any], FakeStdin]:
            bridge, stdin, _reader = self._boot(bmod, tmp_path, spawn)
            return await bridge.manager_add_host_deps([]), stdin

        out, stdin = asyncio.run(scenario())
        assert out == {"ok": True, "data": {"skipped": True, "exitCode": 0}, "error": None}
        assert stdin.frames == []  # 未 boot、未发帧

    def test_wire_sends_specs_and_returns_envelope(self, bmod: Any, tmp_path: Path,
                                                   spawn: SpawnStub) -> None:
        async def scenario() -> tuple[dict[str, Any], FakeStdin]:
            bridge, stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            payload = {"ok": True, "data": {"exitCode": 0, "output": ""}, "error": None}
            reader.feed_data(_wire_line(_response_frame(2, payload)))
            return await bridge.manager_add_host_deps(["@deepseek-ai/dsh-settings@0.2.1-rc.5"]), stdin

        out, stdin = asyncio.run(scenario())
        assert out["ok"] is True
        assert out["data"]["exitCode"] == 0
        assert stdin.frames[1]["method"] == "plugin/host-dep/add"
        assert stdin.frames[1]["params"] == {
            "specs": ["@deepseek-ai/dsh-settings@0.2.1-rc.5"]}

    def test_timeout_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                     spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, _reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            return await bridge.manager_add_host_deps(["@deepseek-ai/dsh-settings@1"], timeout_s=0.01)

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "timeout" in out["error"].lower()

    def test_boot_failure_is_fail_visible(self, bmod: Any, tmp_path: Path) -> None:
        gone = tmp_path / "gone"
        bridge = bmod.DshRuntimeBridge(runtime_root=str(gone))
        out = asyncio.run(bridge.manager_add_host_deps(["@deepseek-ai/dsh-settings@1"]))
        assert out["ok"] is False
        assert "boot failed" in out["error"]

    def test_non_object_result_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                               spawn: SpawnStub) -> None:
        async def scenario() -> dict[str, Any]:
            bridge, _stdin, reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            reader.feed_data(_wire_line(_response_frame(2, ["not", "object"])))
            return await bridge.manager_add_host_deps(["@deepseek-ai/dsh-settings@1"])

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "non-object" in out["error"]

    def test_request_error_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                          spawn: SpawnStub) -> None:
        """初始化成功后 RPC 通道故障（管道断）→ 信封化失败，不向上抛。"""

        async def scenario() -> dict[str, Any]:
            bridge, _stdin, _reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()

            async def boom(method: str, params: dict[str, Any],
                           timeout_s: float | None = None) -> dict[str, Any]:
                raise RuntimeError("pipe broken")

            bridge._request = boom  # type: ignore[method-assign]
            return await bridge.manager_add_host_deps(["@deepseek-ai/dsh-settings@1"])

        out = asyncio.run(scenario())
        assert out["ok"] is False
        assert "pipe broken" in out["error"]

    def test_initialize_exposes_bundle_states(self, bmod: Any, tmp_path: Path,
                                              spawn: SpawnStub) -> None:
        """serverInfo.bundleStates → bridge.bundle_states（管理页运行态来源）。"""

        async def scenario() -> Any:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {
                "tools": _INIT_TOOLS,
                "serverInfo": {"bundleStates": [
                    {"id": "dsh-context", "name": "dsh-context",
                     "state": 0, "label": "pending", "injects": ["sessions"]},
                ]},
            })))
            await bridge.initialize()
            return bridge

        bridge = asyncio.run(scenario())
        assert bridge.bundle_states == [{
            "id": "dsh-context", "name": "dsh-context", "state": 0,
            "label": "pending", "injects": ["sessions"]}]

    def test_initialize_bundle_states_defaults_empty(self, bmod: Any, tmp_path: Path,
                                                     spawn: SpawnStub) -> None:
        async def scenario() -> Any:
            bridge, _stdin, _reader = self._boot(bmod, tmp_path, spawn)
            await bridge.initialize()
            return bridge

        assert asyncio.run(scenario()).bundle_states == []

    def test_initialize_exposes_provider_states(self, bmod: Any, tmp_path: Path,
                                                spawn: SpawnStub) -> None:
        """serverInfo.providerStates → bridge.provider_states（宿主服务提供包挂载态）。"""

        async def scenario() -> Any:
            reader = asyncio.StreamReader()
            stdin = FakeStdin()
            spawn.make(stdin=stdin, stdout=reader, stderr=None)
            bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
            reader.feed_data(_wire_line(_response_frame(1, {
                "tools": _INIT_TOOLS,
                "serverInfo": {"providerStates": [
                    {"name": "@deepseek-ai/cordis-plugin-timer", "state": 2,
                     "label": "active", "injects": []},
                ]},
            })))
            await bridge.initialize()
            return bridge

        bridge = asyncio.run(scenario())
        assert bridge.provider_states == [{
            "name": "@deepseek-ai/cordis-plugin-timer", "state": 2,
            "label": "active", "injects": []}]


# ── 一次性 manager 进程（停桥后装/卸，防 pnpm EPERM；2026-11-09 dev 实测）──


class _OnceProc:
    """communicate() 形态的一次性子进程替身。"""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0,
                 hang: bool = False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False
        self._hang = hang

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(30)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


class TestManagerOneshot:
    def _patch_spawn(self, monkeypatch: pytest.MonkeyPatch, proc: Any,
                     captured: dict[str, Any] | None = None) -> None:
        async def fake_spawn(program: str, *args: str, **kwargs: Any) -> Any:
            if captured is not None:
                captured["program"] = program
                captured["args"] = args
                captured["kwargs"] = kwargs
                request_path = Path(args[2])
                captured["request"] = json.loads(request_path.read_text(encoding="utf-8"))
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    def test_success_parses_envelope_and_passes_request(self, bmod: Any, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _OnceProc(b'{"ok": true, "data": {"application": "restart-required"}, "error": null}\n')
        captured: dict[str, Any] = {}
        self._patch_spawn(monkeypatch, proc, captured)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path), manager_config={"registry": "r"})
        out = asyncio.run(bridge.manager_call_oneshot(
            "install", {"spec": "pkg@1.0.0", "options": {"saveExact": True}}))
        assert out["ok"] is True
        assert out["data"]["application"] == "restart-required"
        assert captured["program"] == "node"
        assert captured["args"][1] == "--manager-once"
        assert captured["request"]["method"] == "plugin/manager/install"
        assert captured["request"]["params"]["spec"] == "pkg@1.0.0"
        assert captured["request"]["manager"] == {"registry": "r"}

    def test_missing_runtime_root_fails_before_spawn(self, bmod: Any,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[str] = []

        async def fake_spawn(*args: Any, **kwargs: Any) -> Any:  # noqa: ARG001
            called.append("spawn")
            return _OnceProc(b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        bridge = bmod.DshRuntimeBridge(runtime_root="")
        out = asyncio.run(bridge.manager_call_oneshot("install", {}))
        assert out["ok"] is False
        assert "AGENTOS_DSH_RUNTIME_ROOT" in out["error"]
        assert called == []

    def test_node_missing_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_spawn(*args: Any, **kwargs: Any) -> Any:  # noqa: ARG001
            raise FileNotFoundError("node")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(bridge.manager_call_oneshot("install", {}))
        assert out["ok"] is False
        assert "node_missing" in out["error"]

    def test_timeout_kills_process(self, bmod: Any, tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _OnceProc(b"", hang=True)
        self._patch_spawn(monkeypatch, proc)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(bridge.manager_call_oneshot("install", {}, timeout_s=0.01))
        assert out["ok"] is False
        assert "timeout" in out["error"]
        assert proc.killed is True

    def test_empty_output_and_stderr_tail(self, bmod: Any, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _OnceProc(b"", b"boom\n", returncode=3)
        self._patch_spawn(monkeypatch, proc)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(bridge.manager_call_oneshot("remove", {"name": "x"}))
        assert out["ok"] is False
        assert "no result" in out["error"]
        assert "boom" in out["error"]

    def test_bad_json_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _OnceProc(b"not-json\n")
        self._patch_spawn(monkeypatch, proc)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(bridge.manager_call_oneshot("install", {}))
        assert out["ok"] is False
        assert "bad result" in out["error"]

    def test_non_object_result_is_fail_visible(self, bmod: Any, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _OnceProc(b"[1,2,3]\n")
        self._patch_spawn(monkeypatch, proc)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(bridge.manager_call_oneshot("install", {}))
        assert out["ok"] is False
        assert "non-object" in out["error"]

    def test_isolated_helpers_delegate(self, bmod: Any, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _OnceProc(b'{"ok": true, "data": {}, "error": null}\n')
        captured: dict[str, Any] = {}
        self._patch_spawn(monkeypatch, proc, captured)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        asyncio.run(bridge.manager_install_isolated("pkg@1.0.0", "req-1", {"saveExact": True}))
        assert captured["request"]["method"] == "plugin/manager/install"
        assert captured["request"]["params"]["requestId"] == "req-1"
        asyncio.run(bridge.manager_remove_isolated("pkg"))
        assert captured["request"]["method"] == "plugin/manager/remove"
        assert captured["request"]["params"] == {"name": "pkg"}

    def test_unlink_failure_does_not_break_result(self, bmod: Any, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _OnceProc(b'{"ok": true, "data": {}, "error": null}\n')
        self._patch_spawn(monkeypatch, proc)

        def boom(_path: str) -> None:
            raise OSError("locked")

        monkeypatch.setattr(bmod.os, "unlink", boom)
        bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
        out = asyncio.run(bridge.manager_call_oneshot("install", {}))
        assert out["ok"] is True
