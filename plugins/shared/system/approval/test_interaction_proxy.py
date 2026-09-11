"""interaction HTTP 面转发行为：human-interaction 桥代理（_HumanInteractionCapabilityProxy）。

行为契约：
- 转发走 human-interaction capability 桥（method 直通，get_pending/respond/cancel）；
- respond 载荷为 (request_id, response) 透传形态，契约由 human 侧自持；
- human 侧显式 error dict 收敛为 RuntimeError（调用方按 False/500 降级）；
- 桥不可用降级：pending 恒空、响应 False，前端轮询契约不破坏。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

import server as mod


class _FakeBridge:
    """human-interaction 桥替身（跨进程外部依赖）：记录转发并回放预设结果。"""

    def __init__(self, result: Any, *, fail: bool = False) -> None:
        self._result = result
        self._fail = fail
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params, timeout))
        if self._fail:
            raise RuntimeError(f"kernel capability call failed: {method}")
        return self._result


def _proxy(bridge: _FakeBridge) -> mod._HumanInteractionCapabilityProxy:
    return mod._HumanInteractionCapabilityProxy(bridge)


@pytest.mark.asyncio
async def test_get_pending_forwards_over_bridge() -> None:
    bridge = _FakeBridge({"requests": [{"id": "r1"}], "count": 1})
    items = await _proxy(bridge).get_pending_requests(limit=50)
    assert items == [{"id": "r1"}]
    assert bridge.calls == [("get_pending", {"session_id": None, "limit": 50}, bridge.calls[0][2])]
    assert bridge.calls[0][2] is not None and bridge.calls[0][2] > 30, (
        "审批交互面转发必须显式传大传输超时（SDK 默认 30s 会误断）"
    )


@pytest.mark.asyncio
async def test_respond_sends_nested_response_envelope() -> None:
    bridge = _FakeBridge({"ok": True})
    ok = await _proxy(bridge).submit_response(
        request_id="r9",
        response_type="approved",
        selected_option="approve",
        feedback="ok",
    )
    assert ok is True
    method, params, _ = bridge.calls[0]
    assert method == "respond"
    assert params["request_id"] == "r9"
    assert params["response"] == {
        "response_type": "approved",
        "selected_option": "approve",
        "answers": None,
        "feedback": "ok",
    }


@pytest.mark.asyncio
async def test_bridge_error_dict_raises() -> None:
    bridge = _FakeBridge({"error": "service not initialized"})
    with pytest.raises(RuntimeError, match="interaction.get_pending"):
        await _proxy(bridge)._call("get_pending", {})


@pytest.mark.asyncio
async def test_bridge_unavailable_degrades_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(cap: str) -> Any:  # noqa: ARG001
        raise KeyError(cap)

    monkeypatch.setattr(mod.plugin, "get_capability", _raise)
    assert mod._get_human_interaction_service() is None
    resp = await mod.http_handle(
        path="/ext/approval_service/interaction/pending",
        method="GET",
    )
    # http.handle 返回 ToolExecutionResult{success, data: HttpHandleResponse}
    # （body 为 base64 JSON，见 plugins/shared/http_json.py 契约）。
    assert resp["success"] is True
    assert resp["data"]["status"] == 200
    body = json.loads(base64.b64decode(resp["data"]["body"]))
    assert body == {"items": [], "total": 0}, "桥不可用时 pending 必须降级空集"
