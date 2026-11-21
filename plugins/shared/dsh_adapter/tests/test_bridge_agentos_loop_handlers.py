# @feature: FP-0.2.可观测性 dsh_adapter DSH兼容四件垫片 | @ci: python-coverage
"""loop handler 真实语义车道（垫片四宿主端，任务 d66b3780c7a9）。

前三件垫片 + 反向 RPC 传输面已由 test_bridge_agentos_dispatch.py 覆盖；
本车道覆盖**宿主侧真实 loop handler**的语义实现：

1. `loop.status(task_id)` → `service-registry.pipeline-runs.list_by_pipeline`
   读 run_status，投影 DSH 可消费的形态（run_status/run_id/exists）；
2. `loop.stop(task_id)` → `pipeline-executor.suspend_pipeline` 停止运行；
3. `loop.inject(task_id, message)` → `chat.send_message` 注入用户消息
   （pipeline_id 非空即注入分支，与前端同一派发链）；
4. caller 未装配 → fail-visible `RuntimeError`（不再 "no agentos handler"）；
5. handler 表内置注册（`__init__` 后三键即存在，无需外部注册）；
6. server.py 装配点 `_wire_kernel_caller` 把 SDK 能力句柄接进桥。

内核 caller 以 SpyCaller 桩承载（真实端到端见 test_bridge_roundtrip_real.py）；
桩只替换「内核 capability 调用」这一个外部边界，handler 语义逻辑全真实。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_bridge_agentos_dispatch import (
    FakeStdin,
    SpawnStub,
    _load_bridge_module,
    _upstream_frame,
    _wait_until,
    _wire_line,
)

PLUGIN_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def bmod() -> Any:
    return _load_bridge_module()


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> SpawnStub:
    stub = SpawnStub()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", stub)
    return stub


class SpyCaller:
    """内核 capability caller 替身：记录调用、按 method 返回预设响应。

    raise_on：设定后调用该 method 直接抛 RuntimeError（模拟内核错误）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.raise_on: dict[str, str] = {}

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method in self.raise_on:
            raise RuntimeError(self.raise_on[method])
        return self.responses[method]

    def respond(self, method: str, value: Any) -> None:
        self.responses[method] = value


def _run_row(status: str, run_id: str, started: str = "2026-11-21T00:00:00Z") -> dict[str, Any]:
    """RunRecord wire 形态（serde lowercase；ended_at 缺省缺席）。"""
    return {
        "run_id": run_id,
        "config_hash": "h",
        "status": status,
        "tenant_id": "default",
        "created_at": started,
        "current_branch": "main",
        "current_seq": 0,
        "pipeline_id": "0123456789abcdef",
    }


# ── AC1: loop.status 真实语义 ────────────────────────────────────────────


class TestLoopStatus:
    def test_status_running_run_projects_wire_shape(self, bmod: Any) -> None:
        """有 running run → 投影 run_status/run_id（数据来自内核快照行）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        spy.respond(
            "service-registry.pipeline-runs.list_by_pipeline",
            [_run_row("running", "run-aaa")],
        )
        bridge.set_kernel_caller(spy)

        out = asyncio.run(bridge._handle_loop_status("0123456789ABCDEF"))  # noqa: SLF001

        assert spy.calls == [(
            "service-registry.pipeline-runs.list_by_pipeline",
            {"pipeline_id": "0123456789abcdef"},
        )], "status 必须按归一化后的 pipeline_id 查内核运行快照"
        assert out["exists"] is True
        assert out["run_status"] == "running", "run_status 必须取自内核快照行（wire lowercase 形态）"
        assert out["run_id"] == "run-aaa"
        assert out["pipeline_id"] == "0123456789abcdef"

    def test_status_no_run_is_idle_shape(self, bmod: Any) -> None:
        """无运行快照行 → idle 形态（exists=False），不是错误。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        spy.respond("service-registry.pipeline-runs.list_by_pipeline", [])
        bridge.set_kernel_caller(spy)

        out = asyncio.run(bridge._handle_loop_status("pipeline-x"))  # noqa: SLF001

        assert out == {
            "pipeline_id": "pipeline-x",
            "exists": False,
            "run_status": None,
            "run_id": None,
        }

    def test_status_kernel_error_is_fail_visible(self, bmod: Any) -> None:
        """内核调用失败 → RuntimeError 原样传播（fail-visible，禁止吞成 ok 值）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        spy.raise_on["service-registry.pipeline-runs.list_by_pipeline"] = "db exploded"
        bridge.set_kernel_caller(spy)

        with pytest.raises(RuntimeError, match="db exploded"):
            asyncio.run(bridge._handle_loop_status("pipeline-x"))  # noqa: SLF001

    def test_status_empty_task_id_rejected(self, bmod: Any) -> None:
        """空 task_id → ValueError（fail-visible，不透传空坐标）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        bridge.set_kernel_caller(spy)

        with pytest.raises(ValueError, match="task_id"):
            asyncio.run(bridge._handle_loop_status("   "))  # noqa: SLF001
        assert spy.calls == [], "拒绝必须发生在内核调用之前"


# ── AC2: loop.stop 真实语义 ──────────────────────────────────────────────


class TestLoopStop:
    def test_stop_calls_suspend_pipeline_and_projects_result(self, bmod: Any) -> None:
        """stop → pipeline-executor.suspend_pipeline（内核侧幂等）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        spy.respond("pipeline-executor.suspend_pipeline", {
            "status": "suspended", "pipeline_id": "pipe-1", "run_id": "run-bbb",
        })
        bridge.set_kernel_caller(spy)

        out = asyncio.run(bridge._handle_loop_stop("pipe-1"))  # noqa: SLF001

        assert spy.calls == [("pipeline-executor.suspend_pipeline", {"pipeline_id": "pipe-1"})]
        assert out["ok"] is True
        assert out["run_status"] == "suspended"
        assert out["run_id"] == "run-bbb"

    def test_stop_kernel_error_propagates(self, bmod: Any) -> None:
        """停止失败 → RuntimeError 传播（桥层翻译成 ok=False 错误帧）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        spy.raise_on["pipeline-executor.suspend_pipeline"] = "store locked"
        bridge.set_kernel_caller(spy)

        with pytest.raises(RuntimeError, match="store locked"):
            asyncio.run(bridge._handle_loop_stop("pipe-1"))  # noqa: SLF001


# ── AC3: loop.inject 真实语义 ────────────────────────────────────────────


class TestLoopInject:
    def test_inject_string_message_sends_chat_send_message(self, bmod: Any) -> None:
        """str 消息 → chat.send_message（required message+user_id 全带）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        spy.respond("chat.send_message", {"status": "dispatched", "pipeline_id": "pipe-9"})
        bridge.set_kernel_caller(spy)

        out = asyncio.run(bridge._handle_loop_inject("pipe-9", "please continue"))  # noqa: SLF001

        assert spy.calls == [(
            "chat.send_message",
            {"pipeline_id": "pipe-9", "message": "please continue", "user_id": "agentos-loop-inject"},
        )], "注入必须走 chat.send_message（pipeline_id 非空即注入分支，与前端同一派发链）"
        assert out["ok"] is True
        assert out["injected"] is True

    def test_inject_structured_payload_serializes_to_text(self, bmod: Any) -> None:
        """dict 载荷 → JSON 文本注入（DSH 侧结构化 additionalContext 场景）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        spy.respond("chat.send_message", {"status": "dispatched", "pipeline_id": "pipe-9"})
        bridge.set_kernel_caller(spy)

        asyncio.run(bridge._handle_loop_inject("pipe-9", {"hint": "retry", "n": 2}))  # noqa: SLF001

        (method, params), = spy.calls
        assert method == "chat.send_message"
        assert json.loads(params["message"]) == {"hint": "retry", "n": 2}

    def test_inject_empty_payload_rejected(self, bmod: Any) -> None:
        """空载荷 → ValueError（不派发空消息轮）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy = SpyCaller()
        bridge.set_kernel_caller(spy)

        with pytest.raises(ValueError, match="payload"):
            asyncio.run(bridge._handle_loop_inject("pipe-9", ""))  # noqa: SLF001
        assert spy.calls == []


# ── AC4: caller 未装配 / 装配形态 ────────────────────────────────────────


class TestCallerWiring:
    def test_unwired_caller_is_fail_visible_not_no_handler(self, bmod: Any) -> None:
        """caller 未装配 → fail-visible，错误不再是 "no agentos handler"（AC1 否定面）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        with pytest.raises(RuntimeError, match="kernel caller not wired"):
            asyncio.run(bridge._handle_loop_status("pipe-1"))  # noqa: SLF001

    def test_builtin_handlers_registered_by_init(self, bmod: Any) -> None:
        """__init__ 后 handler 表三键即存在（无需外部注册；消灭 no agentos handler 缺省）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        assert set(bridge._agentos_handlers) >= {"loop.status", "loop.stop", "loop.inject"}  # noqa: SLF001

    def test_builtin_handler_reachable_via_real_read_loop(
        self, bmod: Any, tmp_path: Path, spawn: SpawnStub
    ) -> None:
        """上行帧全链（真实读循环 → 内置 handler → 错误帧回写）。

        caller 未装配：响应必须是 ok=False + "kernel caller not wired"——
        证明内置注册生效且 fail-visible 语义正确（区别于旧 "no agentos handler"）。
        """
        from test_bridge_agentos_dispatch import _boot

        async def scenario() -> FakeStdin:
            bridge, stdin, reader = _boot(bmod, tmp_path, spawn)
            bridge.set_kernel_caller(None)
            await bridge.initialize()
            reader.feed_data(_wire_line(_upstream_frame("up-9", "loop.status", ["pipe-1"])))
            await _wait_until(lambda: any(f.get("id") == "up-9" for f in stdin.frames), timeout_s=2.0)
            return stdin

        stdin = asyncio.run(scenario())
        resp = next(f for f in stdin.frames if f.get("id") == "up-9")
        assert resp["result"]["ok"] is False
        assert "kernel caller not wired" in resp["result"]["error"]

    def test_set_kernel_caller_accepts_sync_caller(self, bmod: Any) -> None:
        """同步 caller（返回值非 awaitable）同样可用（_kernel_call 双路径）。"""
        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        calls: list[tuple[str, dict[str, Any]]] = []

        def sync_caller(method: str, params: dict[str, Any]) -> Any:
            calls.append((method, params))
            return [_run_row("suspended", "run-ccc")]

        bridge.set_kernel_caller(sync_caller)
        out = asyncio.run(bridge._handle_loop_status("pipe-2"))  # noqa: SLF001
        assert out["run_status"] == "suspended"
        assert calls


# ── AC4: server.py 装配点 ────────────────────────────────────────────────


class TestServerWiring:
    def test_wire_kernel_caller_routes_three_capabilities(self, bmod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """SDK 能力句柄 → 统一 caller 装配：三个前缀各走各的内核能力。"""
        srv_mod = _load_server_module()

        class FakeHandle:
            def __init__(self, sink: dict[str, Any]) -> None:
                self._sink = sink

            async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
                self._sink["last"] = (method, params)
                return self._sink["reply"].get(method, [])

        sink: dict[str, Any] = {
            "reply": {
                "pipeline-runs.list_by_pipeline": [],
                "suspend_pipeline": {"status": "suspended", "pipeline_id": "pipe-1", "run_id": "run-x"},
                "send_message": {"status": "dispatched", "pipeline_id": "pipe-1"},
            },
        }
        handles = {
            "service-registry": FakeHandle(sink),
            "pipeline-executor": FakeHandle(sink),
            "chat": FakeHandle(sink),
        }

        class FakePlugin:
            def get_capability(self, name: str) -> Any:
                if name not in handles:
                    raise KeyError(name)
                return handles[name]

        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        spy_plugin = FakePlugin()
        srv_mod._wire_kernel_caller(bridge, spy_plugin)  # noqa: SLF001

        # 三条能力前缀都要能路由：status→service-registry，stop→pipeline-executor，inject→chat
        asyncio.run(bridge._handle_loop_status("pipe-1"))  # noqa: SLF001
        assert sink["last"][0] == "pipeline-runs.list_by_pipeline", \
            "status 必须路由到 service-registry 能力句柄（剥前缀短名）"
        asyncio.run(bridge._handle_loop_stop("pipe-1"))  # noqa: SLF001
        assert sink["last"][0] == "suspend_pipeline"
        asyncio.run(bridge._handle_loop_inject("pipe-1", "m"))  # noqa: SLF001
        assert sink["last"][0] == "send_message", "inject 经 chat 能力句柄以短名调用"

    def test_wire_kernel_caller_partial_caps_still_wires_available(self, bmod: Any) -> None:
        """部分能力未注入（KeyError）→ 跳过缺席者，可用者照常装配。"""
        srv_mod = _load_server_module()

        class FakeHandle:
            async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
                return []

        class PartialPlugin:
            def get_capability(self, name: str) -> Any:
                if name == "service-registry":
                    return FakeHandle()
                raise KeyError(name)

        bridge = bmod.DshRuntimeBridge(runtime_root="/tmp/x")
        srv_mod._wire_kernel_caller(bridge, PartialPlugin())  # noqa: SLF001
        out = asyncio.run(bridge._handle_loop_status("pipe-3"))  # noqa: SLF001
        assert out["exists"] is False, "仅有 service-registry 时 status 照常工作"

        with pytest.raises(RuntimeError, match="kernel capability not injected"):
            asyncio.run(bridge._handle_loop_stop("pipe-3"))  # noqa: SLF001
        with pytest.raises(RuntimeError, match="kernel capability not injected"):
            asyncio.run(bridge._handle_loop_inject("pipe-3", "m"))  # noqa: SLF001


def _load_server_module() -> Any:
    """按文件路径加载 server.py（与 test_dsh_live_handlers 同手法，防裸名劫持）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dsh_adapter_server_loop_test", PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dsh_adapter_server_loop_test"] = module
    spec.loader.exec_module(module)
    return module
