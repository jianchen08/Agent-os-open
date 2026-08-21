# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""approval 插件 interaction 域 7 端点测试（channel_api 退役批次 2 自持承接）。

覆盖 /ext/approval_service/interaction/**（源 routes_missing.py interaction_router）：
1. GET pending —— 代理 get_pending_requests → {items, total}
2. POST response —— 嵌套/扁平响应形态 unwrap → {success}；缺 request_id → 400
3. GET {request_id} —— 详情 / 404
4. POST approve / deny —— submit_response(approved/denied) → status 回显 + feedback
5. POST cancel —— cancel_request(reason) → cancelled
6. POST viewed —— human sidecar 无 viewed 工具，确认应答（viewed: True）
7. tool-executor 代理链路 —— invoke 载荷（tool_name/args）+ 信封解包 + 86500 超时
8. tool-executor 未注入降级 —— pending 空 / 变更类 success False（前端契约不破坏）
9. 404 未知路由 / 非法 JSON body 边界
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "approval"


def _load_server() -> Any:
    """动态加载 approval/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "approval_interaction_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["approval_interaction_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(server: Any, path: str, method: str = "GET", raw_body: str = "") -> dict[str, Any]:
    return _run(server.http_handle(path=path, method=method, raw_body=raw_body))


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeService:
    """fake human-interaction 服务：可插桩各方法。"""

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.responds: list[tuple[str, dict[str, Any]]] = []
        self.submits: list[tuple[str, str, dict[str, Any]]] = []
        self.cancels: list[tuple[str, str | None]] = []
        self.viewed: list[str] = []

    async def get_pending_requests(self, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.pending

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        for it in self.pending:
            if it.get("id") == request_id or it.get("request_id") == request_id:
                return it
        return None

    async def respond(self, request_id: str, resp_data: dict[str, Any]) -> bool:
        self.responds.append((request_id, resp_data))
        return True

    async def submit_response(
        self, request_id: str, response_type: str, selected_option: str | None = None,
        answers: list[str] | None = None, feedback: str | None = None, user_id: str | None = None,
    ) -> bool:
        self.submits.append((request_id, response_type, {
            "selected_option": selected_option, "answers": answers, "feedback": feedback,
        }))
        return True

    async def cancel_request(self, request_id: str, reason: str | None = None) -> bool:
        self.cancels.append((request_id, reason))
        return True

    async def mark_as_viewed(self, request_id: str) -> bool:
        self.viewed.append(request_id)
        return True


def _inject_service(server: Any, service: FakeService) -> None:
    server._get_human_interaction_service = lambda: service


# ── GET /pending ──────────────────────────────────────────────────────────


def test_pending_returns_items(server: Any) -> None:
    svc = FakeService()
    svc.pending = [{"id": "r1", "session_id": "s1", "message_data": {"request_id": "r1"}}]
    _inject_service(server, svc)

    status, body = _decode(_call(server, "/ext/approval_service/interaction/pending"))

    assert status == 200
    assert body["total"] == 1
    assert body["items"][0]["id"] == "r1"


def test_pending_degrades_empty(server: Any) -> None:
    """tool-executor 未注入（服务 None）→ 200 空列表（前端轮询契约不破坏）。"""
    server._get_human_interaction_service = lambda: None

    status, body = _decode(_call(server, "/ext/approval_service/interaction/pending"))

    assert status == 200
    assert body == {"items": [], "total": 0}


# ── POST /response ────────────────────────────────────────────────────────


def test_response_flat_body(server: Any) -> None:
    svc = FakeService()
    _inject_service(server, svc)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/response", "POST",
        raw_body=_b64(json.dumps({"request_id": "r1", "selected_option": "ok", "feedback": "go"})),
    ))

    assert status == 200
    assert body == {"success": True}
    rid, resp = svc.responds[0]
    assert rid == "r1"
    # http_handle 把 body 原样交 service.respond（proxy 内做嵌套 unwrap——
    # response_type 归约在代理链路测试覆盖）
    assert resp == {"request_id": "r1", "selected_option": "ok", "feedback": "go"}


def test_response_nested_body(server: Any) -> None:
    svc = FakeService()
    _inject_service(server, svc)

    status, _ = _decode(_call(
        server, "/ext/approval_service/interaction/response", "POST",
        raw_body=_b64(json.dumps({
            "request_id": "r1",
            "response": {"response_type": "answered", "selected_option": "x", "feedback": "f"},
        })),
    ))

    rid, resp = svc.responds[0]
    assert rid == "r1"
    assert resp["response"]["selected_option"] == "x"


def test_response_missing_request_id_400(server: Any) -> None:
    _inject_service(server, FakeService())

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/response", "POST", raw_body=_b64("{}"),
    ))

    assert status == 400
    assert body["detail"] == "缺少 request_id"


# ── GET /{request_id} ─────────────────────────────────────────────────────


def test_get_interaction_found(server: Any) -> None:
    svc = FakeService()
    svc.pending = [{"id": "r9", "session_id": "s9", "message_data": {"request_id": "r9"}}]
    _inject_service(server, svc)

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r9"))

    assert status == 200
    assert body["id"] == "r9"


def test_get_interaction_not_found_404(server: Any) -> None:
    _inject_service(server, FakeService())

    status, body = _decode(_call(server, "/ext/approval_service/interaction/nope"))

    assert status == 404
    assert body["detail"] == "交互请求不存在"


# ── POST approve / deny ───────────────────────────────────────────────────


def test_approve(server: Any) -> None:
    svc = FakeService()
    _inject_service(server, svc)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST",
        raw_body=_b64(json.dumps({"feedback": "同意"})),
    ))

    assert status == 200
    assert body == {"success": True, "request_id": "r1", "status": "approved"}
    rid, rtype, kw = svc.submits[0]
    assert (rid, rtype) == ("r1", "approved")
    assert kw["selected_option"] == "approve"
    assert kw["feedback"] == "同意"


def test_deny(server: Any) -> None:
    svc = FakeService()
    _inject_service(server, svc)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/deny", "POST", raw_body=_b64("{}"),
    ))

    assert status == 200
    assert body == {"success": True, "request_id": "r1", "status": "denied"}
    rid, rtype, kw = svc.submits[0]
    assert (rid, rtype) == ("r1", "denied")
    assert kw["selected_option"] == "reject"


def test_approve_degraded_false(server: Any) -> None:
    server._get_human_interaction_service = lambda: None

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST", raw_body=_b64("{}"),
    ))

    assert status == 200
    assert body == {"success": False, "request_id": "r1", "status": "approved"}


# ── POST cancel ───────────────────────────────────────────────────────────


def test_cancel(server: Any) -> None:
    svc = FakeService()
    _inject_service(server, svc)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/cancel", "POST",
        raw_body=_b64(json.dumps({"reason": "用户取消"})),
    ))

    assert status == 200
    assert body == {"success": True, "request_id": "r1", "status": "cancelled"}
    assert svc.cancels == [("r1", "用户取消")]


# ── POST viewed（human sidecar 无 viewed 工具，确认应答）──────────────────


def test_viewed_acknowledgement(server: Any) -> None:
    svc = FakeService()
    _inject_service(server, svc)

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r1/viewed", "POST"))

    assert status == 200
    assert body == {"success": True, "request_id": "r1", "viewed": True}
    assert svc.viewed == ["r1"]


# ── tool-executor 代理链路（真实代理 + fake capability）────────────────────


class FakeToolExecutor:
    """fake tool-executor capability：记录 invoke 载荷、按工具名返回信封。"""

    def __init__(self, responses: dict[str, Any], calls: list[tuple[dict[str, Any], float | None]]) -> None:
        self._responses = responses
        self._calls = calls

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        assert method == "invoke"
        self._calls.append((params, timeout))
        tool = params.get("tool_name")
        return self._responses.get(tool, {})


def test_proxy_pending_via_tool_executor(server: Any) -> None:
    calls: list[tuple[dict[str, Any], float | None]] = []
    executor = FakeToolExecutor({
        # 平铺形态（内核 capability 结果直接平铺——approval.create_choice 等
        # 既有消费方均按平铺 key 读取）
        "interaction.get_pending": {"requests": [{"id": "r1"}], "count": 1},
    }, calls)
    server.plugin._capabilities["tool-executor"] = executor

    status, body = _decode(_call(server, "/ext/approval_service/interaction/pending"))

    assert status == 200
    assert body == {"items": [{"id": "r1"}], "total": 1}
    params, timeout = calls[0]
    assert params["tool_name"] == "interaction.get_pending"
    assert params["args"] == {"session_id": None, "limit": 50}
    assert timeout == 86500.0  # 长等待超时（对齐 wait_for_choice 业务超时）


def test_proxy_unwrap_data_envelope(server: Any) -> None:
    """data 包裹形态也能解包（源 routes_missing 原序会读空，随迁时修正）。"""
    executor = FakeToolExecutor({
        "interaction.get_pending": {"data": {"requests": [{"id": "r1"}], "count": 1}},
    }, [])
    server.plugin._capabilities["tool-executor"] = executor

    status, body = _decode(_call(server, "/ext/approval_service/interaction/pending"))

    assert status == 200
    assert body == {"items": [{"id": "r1"}], "total": 1}


def test_proxy_approve_via_tool_executor(server: Any) -> None:
    calls: list[tuple[dict[str, Any], float | None]] = []
    executor = FakeToolExecutor({
        "interaction.respond": {"ok": True, "request_id": "r1", "status": "submitted"},
    }, calls)
    server.plugin._capabilities["tool-executor"] = executor

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST", raw_body=_b64("{}"),
    ))

    assert status == 200
    assert body["success"] is True
    params, _ = calls[0]
    assert params["tool_name"] == "interaction.respond"
    args = params["args"]
    assert args["request_id"] == "r1"
    assert args["response_type"] == "approved"
    assert args["selected_option"] == "approve"


def test_proxy_cancel_via_tool_executor(server: Any) -> None:
    calls: list[tuple[dict[str, Any], float | None]] = []
    executor = FakeToolExecutor({
        "interaction.cancel": {"data": {"ok": True, "request_id": "r1", "status": "cancelled"}},
    }, calls)
    server.plugin._capabilities["tool-executor"] = executor

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/cancel", "POST",
        raw_body=_b64(json.dumps({"reason": "x"})),
    ))

    assert status == 200
    assert body["status"] == "cancelled"
    params, _ = calls[0]
    assert params["tool_name"] == "interaction.cancel"
    assert params["args"] == {"request_id": "r1", "reason": "x"}


def test_proxy_tool_error_is_false(server: Any) -> None:
    """工具返回 error 信封 → 变更类端点 success False（转发失败不崩）。"""
    executor = FakeToolExecutor({
        "interaction.respond": {"error": "service not initialized"},
    }, [])
    server.plugin._capabilities["tool-executor"] = executor

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST", raw_body=_b64("{}"),
    ))

    assert status == 200
    assert body["success"] is False


# ── 边界 ──────────────────────────────────────────────────────────────────


def test_unknown_route_404(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/approval_service/whatever"))

    assert status == 404


def test_invalid_json_body_400(server: Any) -> None:
    _inject_service(server, FakeService())

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/response", "POST", raw_body=_b64("{bad"),
    ))

    assert status == 400