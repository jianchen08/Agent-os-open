# @feature: FP-0.2.可观测性 dsh_adapter DSH兼容四件垫片 | @ci: python-coverage
"""反向 RPC 车道（垫片四的 Python 落地端）：

1. Node 桥上行请求帧（``agentos/call``）→ 读循环识别 → handler 表分发 →
   响应帧写回（loop.status / loop.stop / loop.inject 的宿主接线点）；
2. 未注册 handler → ``ok=False`` + 可定位错误（fail-visible，不静默）；
3. handler 异常 → 翻译成错误帧回传（不吞异常、不让桥死）；
4. ``emit_hook`` 透传 exec/result/agentKey（三参签名载荷）——缺省参数不破坏
   现有 turn/end 消费面。

打桩约定沿 ``test_bridge_gaps`` E1 车道：``asyncio.create_subprocess_exec``
是唯一外部边界替换点（SpawnStub + FakeStdin + StreamReader），``initialize``
引导后读循环保持存活——上行帧喂入后由真实读循环分发；等待用真实小步轮询。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_bridge_gaps import (  # 复用既有桩基建（同一插件目录的加载器与替身）
    _INIT_TOOLS,
    FakeStdin,
    SpawnStub,
    _load_bridge_module,
    _response_frame,
    _wait_until,
    _wire_line,
)


@pytest.fixture
def bmod() -> Any:
    return _load_bridge_module()


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> SpawnStub:
    stub = SpawnStub()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", stub)
    return stub


def _boot(
    bmod: Any, tmp_path: Path, spawn: SpawnStub
) -> tuple[Any, FakeStdin, asyncio.StreamReader]:
    """E1 同款引导：预喂 initialize 响应帧（id=1），读循环由此保持存活。"""
    reader = asyncio.StreamReader()
    stdin = FakeStdin()
    spawn.make(stdin=stdin, stdout=reader, stderr=None)
    bridge = bmod.DshRuntimeBridge(runtime_root=str(tmp_path))
    reader.feed_data(_wire_line(_response_frame(1, {"tools": _INIT_TOOLS, "serverInfo": {}})))
    return bridge, stdin, reader


def _upstream_frame(req_id: str, method: str, args: list[Any]) -> dict[str, Any]:
    """Node 桥上行请求帧（dsh-compat-shims.mjs 服务代理的线上形态）。"""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "agentos/call",
        "params": {"method": method, "args": args},
    }


def test_upstream_request_dispatches_to_registered_handler_and_writes_response(
    bmod: Any, tmp_path: Path, spawn: SpawnStub
) -> None:
    """垫片四宿主端：注册过的 agentos handler 收到位置实参并回响应帧。"""
    seen_calls: list[tuple[Any, ...]] = []

    async def scenario() -> FakeStdin:
        bridge, stdin, reader = _boot(bmod, tmp_path, spawn)

        async def handler(task_id: str, opts: dict[str, Any] | None = None) -> dict[str, Any]:
            seen_calls.append((task_id, opts))
            return {"run_status": "running", "task": task_id}

        bridge.register_agentos_handler("loop.status", handler)
        await bridge.initialize()
        reader.feed_data(_wire_line(_upstream_frame("up-1", "loop.status", ["task-9", {"force": True}])))
        await _wait_until(lambda: any(f.get("id") == "up-1" for f in stdin.frames), timeout_s=2.0)
        return stdin

    stdin = asyncio.run(scenario())
    resp = next(f for f in stdin.frames if f.get("id") == "up-1")
    assert resp["jsonrpc"] == "2.0"
    # 信封契约：成功/失败同形三键 {ok, data, error}（Node 侧代理按 ok===false 判失败）
    assert resp["result"] == {
        "ok": True,
        "data": {"run_status": "running", "task": "task-9"},
        "error": None,
    }
    assert seen_calls == [("task-9", {"force": True})], "handler 必须按位置收到桥代理转发的实参"


def test_upstream_request_without_handler_is_fail_visible(
    bmod: Any, tmp_path: Path, spawn: SpawnStub
) -> None:
    """未注册的 agentos 方法 → ok=False + 可定位错误（禁止静默丢弃）。"""

    async def scenario() -> FakeStdin:
        bridge, stdin, reader = _boot(bmod, tmp_path, spawn)
        await bridge.initialize()
        reader.feed_data(_wire_line(_upstream_frame("up-2", "loop.restart", ["task-1"])))
        await _wait_until(lambda: any(f.get("id") == "up-2" for f in stdin.frames), timeout_s=2.0)
        return stdin

    stdin = asyncio.run(scenario())
    resp = next(f for f in stdin.frames if f.get("id") == "up-2")
    assert resp["result"]["ok"] is False
    assert "loop.restart" in resp["result"]["error"], "错误必须可定位到缺失的方法名"


def test_upstream_handler_exception_translates_to_error_frame(
    bmod: Any, tmp_path: Path, spawn: SpawnStub
) -> None:
    """handler 抛异常 → 错误帧回传（边界翻译成 fail-visible 信封，桥不死）。"""

    async def scenario() -> FakeStdin:
        bridge, stdin, reader = _boot(bmod, tmp_path, spawn)

        async def boom(*_args: Any) -> None:
            raise ValueError("kernel unreachable")

        bridge.register_agentos_handler("loop.inject", boom)
        await bridge.initialize()
        reader.feed_data(_wire_line(_upstream_frame("up-3", "loop.inject", ["t", "msg"])))
        await _wait_until(lambda: any(f.get("id") == "up-3" for f in stdin.frames), timeout_s=2.0)
        return stdin

    stdin = asyncio.run(scenario())
    resp = next(f for f in stdin.frames if f.get("id") == "up-3")
    assert resp["result"]["ok"] is False
    assert "kernel unreachable" in resp["result"]["error"]


def test_emit_hook_forwards_exec_result_agent_key(
    bmod: Any, tmp_path: Path, spawn: SpawnStub
) -> None:
    """三参签名透传：emit_hook 的 exec/result/agentKey 原样上线（桥按 DSH 签名展开）。"""
    exec_payload = {"callId": "c-1", "name": "grep"}
    result_payload = {"ok": True}

    async def scenario() -> tuple[dict[str, Any], FakeStdin]:
        bridge, stdin, reader = _boot(bmod, tmp_path, spawn)
        await bridge.initialize()
        reader.feed_data(
            _wire_line(
                _response_frame(
                    2,
                    {"ok": True, "event": "tools/post-execute",
                     "decision": {"kind": "accept"}, "error": None},
                )
            )
        )
        out = await bridge.emit_hook(
            "tools/post-execute",
            {},
            exec=exec_payload,
            result=result_payload,
            agent_key="sess-1",
        )
        return out, stdin

    out, stdin = asyncio.run(scenario())
    frame = next(f for f in stdin.frames if f.get("method") == "hook/emit")
    params = frame["params"]
    assert params["event"] == "tools/post-execute"
    assert params["payload"] == {}
    assert params["exec"] == exec_payload
    assert params["result"] == result_payload
    assert params["agentKey"] == "sess-1"
    assert out["decision"] == {"kind": "accept"}


def test_emit_hook_legacy_call_omits_shim_params(
    bmod: Any, tmp_path: Path, spawn: SpawnStub
) -> None:
    """既有消费面（turn/end 词表边界事件）不传新参数 → 帧上不得出现空 shim 键。"""

    async def scenario() -> FakeStdin:
        bridge, stdin, reader = _boot(bmod, tmp_path, spawn)
        await bridge.initialize()
        reader.feed_data(
            _wire_line(
                _response_frame(2, {"ok": True, "event": "turn/end", "decision": None, "error": None})
            )
        )
        await bridge.emit_hook("turn/end", {"reason": "x"})
        return stdin

    stdin = asyncio.run(scenario())
    frame = next(f for f in stdin.frames if f.get("method") == "hook/emit")
    params = frame["params"]
    assert params["event"] == "turn/end"
    assert "exec" not in params, "未传 exec 时帧上不得出现 exec 键（旧形态不带 shim 载荷）"
    assert "result" not in params
    assert "agentKey" not in params
