# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""approval 插件 interaction 域 7 端点测试（channel_api 自持承接）。

覆盖 /ext/approval_service/interaction/**（源 routes_missing.py interaction_router）：
1. GET pending —— 代理 get_pending_requests → {items, total}
2. POST response —— 嵌套/扁平响应形态 unwrap → {success}；缺 request_id → 400
3. GET {request_id} —— 详情 / 404
4. POST approve / deny —— submit_response(approved/denied) → status 回显 + feedback
5. POST cancel —— cancel_request(reason) → cancelled
6. POST viewed —— human sidecar 无 viewed 工具，确认应答（viewed: True）
7. human-interaction 桥代理链路 —— 转发载荷（method/params）+ 86500 传输超时
8. human-interaction 能力缺失降级 —— pending 空 / 变更类 success False（前端契约不破坏）
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


# 内核已认证 /ext 分发注入的身份头（http_dispatcher：X-AgentOS-Tenant/User/Role）。
# approve/deny 归属校验只信这组头；归属不可归因（无创建链路记录）时仅 admin
# 可决策——本文件无创建链路，决策请求按 admin 已认证分发装配。
_ADMIN_HEADERS = {
    "x-agentos-tenant": "t-default",
    "x-agentos-user": "u-admin",
    "x-agentos-role": "admin",
}


def _call(server: Any, path: str, method: str = "GET", raw_body: str = "",
          headers: dict[str, str] | None = None) -> dict[str, Any]:
    # 默认装配内核已认证分发的身份头（M2 后交互面全端点过归属守卫，缺头即
    # 403/空集——那是负例专项（test_approval_ownership.py）的职责，本文件的
    # 路由/代理契约测试一律按已认证分发形态驱动。
    if headers is None:
        headers = _ADMIN_HEADERS
    return _run(server.http_handle(path=path, method=method, raw_body=raw_body, headers=headers))


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
        raw_body=_b64(json.dumps({"feedback": "同意"})), headers=_ADMIN_HEADERS,
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
        server, "/ext/approval_service/interaction/r1/deny", "POST",
        raw_body=_b64("{}"), headers=_ADMIN_HEADERS,
    ))

    assert status == 200
    assert body == {"success": True, "request_id": "r1", "status": "denied"}
    rid, rtype, kw = svc.submits[0]
    assert (rid, rtype) == ("r1", "denied")
    assert kw["selected_option"] == "reject"


def test_approve_degraded_false(server: Any) -> None:
    server._get_human_interaction_service = lambda: None

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST",
        raw_body=_b64("{}"), headers=_ADMIN_HEADERS,
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


# ── human-interaction 桥代理链路（真实代理 + fake capability 桥）────────────


class FakeBridge:
    """fake human-interaction capability 桥：记录转发调用、按方法名回放预设结果。"""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params, timeout))
        return self._results.get(method, {})


def _inject_bridge(server: Any, bridge: FakeBridge) -> None:
    """把 fake 桥挂为 human-interaction 能力（走真实 _get_human_interaction_service 链路）。"""
    server.plugin.get_capability = lambda *_a: bridge


def test_proxy_pending_via_bridge(server: Any) -> None:
    bridge = FakeBridge({"get_pending": {"requests": [{"id": "r1"}], "count": 1}})
    _inject_bridge(server, bridge)

    status, body = _decode(_call(server, "/ext/approval_service/interaction/pending"))

    assert status == 200
    assert body == {"items": [{"id": "r1"}], "total": 1}
    method, params, timeout = bridge.calls[0]
    assert method == "get_pending"
    assert params == {"session_id": None, "limit": 50}
    assert timeout == 86500.0  # 长传输超时（SDK 默认 30s 会误断审批交互面）


def test_proxy_approve_via_bridge(server: Any) -> None:
    bridge = FakeBridge({"respond": {"ok": True, "request_id": "r1", "status": "submitted"}})
    _inject_bridge(server, bridge)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST",
        raw_body=_b64("{}"), headers=_ADMIN_HEADERS,
    ))

    assert status == 200
    assert body["success"] is True
    method, params, _ = bridge.calls[0]
    assert method == "respond"
    assert params["request_id"] == "r1"
    assert params["response"]["response_type"] == "approved"
    assert params["response"]["selected_option"] == "approve"


def test_proxy_cancel_via_bridge(server: Any) -> None:
    bridge = FakeBridge({"cancel": {"ok": True, "request_id": "r1", "status": "cancelled"}})
    _inject_bridge(server, bridge)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/cancel", "POST",
        raw_body=_b64(json.dumps({"reason": "x"})),
    ))

    assert status == 200
    assert body["status"] == "cancelled"
    method, params, _ = bridge.calls[0]
    assert method == "cancel"
    assert params == {"request_id": "r1", "reason": "x"}


def test_proxy_tool_error_is_false(server: Any) -> None:
    """桥回 error 信封 → RuntimeError 收敛 → 变更类端点 success False（转发失败不崩）。"""
    bridge = FakeBridge({"respond": {"error": "service not initialized"}})
    _inject_bridge(server, bridge)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST",
        raw_body=_b64("{}"), headers=_ADMIN_HEADERS,
    ))

    assert status == 200
    assert body["success"] is False


# ── 边界 ──────────────────────────────────────────────────────────────────


def test_proxy_respond_non_dict_body(server: Any) -> None:
    """respond 收到非 dict body → 空 inner → 默认 answered 形状转发。"""
    bridge = FakeBridge({"respond": {"ok": True, "request_id": "r1"}})

    result = _run(server._HumanInteractionCapabilityProxy(bridge).respond("r1", "not-a-dict"))

    assert result is True
    _, params, _ = bridge.calls[0]
    assert params["response"]["response_type"] == "answered"
    assert params["response"]["selected_option"] is None


def test_proxy_non_dict_result_raises_500(server: Any) -> None:
    """桥返回非 dict（如字符串）→ AttributeError → http 500（响应可解析）。"""
    bridge = FakeBridge({"get_pending": "oops-not-a-dict"})
    _inject_bridge(server, bridge)

    status, body = _decode(_call(server, "/ext/approval_service/interaction/pending"))

    assert status == 500
    assert "internal server error" in body["error"]


def test_degraded_via_missing_capability(server: Any) -> None:
    """plugin 无 human-interaction 能力 → 真实 _get_human_interaction_service 降级
    （不 monkeypatch 函数本体，验证 except 分支 + 空 pending 语义）。"""
    status, body = _decode(_call(server, "/ext/approval_service/interaction/pending"))

    assert status == 200
    assert body == {"items": [], "total": 0}

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/approve", "POST", headers=_ADMIN_HEADERS))
    assert status == 200
    assert body["success"] is False

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r1"))
    assert status == 404


def test_get_request_matches_by_request_id_key(server: Any) -> None:
    """详情匹配兼容 id 与 request_id 两种键（get_request 循环两个分支）。"""
    svc = FakeService()
    svc.pending = [
        {"request_id": "rid-only", "session_id": "s"},
        {"id": "id-only", "session_id": "s2", "message_data": {"request_id": "id-only"}},
    ]
    _inject_service(server, svc)

    _, body = _decode(_call(server, "/ext/approval_service/interaction/rid-only"))
    assert body["request_id"] == "rid-only"

    _, body = _decode(_call(server, "/ext/approval_service/interaction/id-only"))
    assert body["id"] == "id-only"


def test_proxy_get_detail_via_bridge(server: Any) -> None:
    """真实代理 get_request（pending 过滤匹配）经 human-interaction 桥链路。"""
    bridge = FakeBridge({
        "get_pending": {"requests": [{"request_id": "r-x", "session_id": "s"}], "count": 1},
    })
    _inject_bridge(server, bridge)

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r-x"))

    assert status == 200
    assert body["request_id"] == "r-x"

    status, _ = _decode(_call(server, "/ext/approval_service/interaction/nope"))
    assert status == 404


def test_proxy_respond_via_bridge(server: Any) -> None:
    """真实代理 respond（嵌套 unwrap）→ (request_id, response) 透传载荷。"""
    bridge = FakeBridge({"respond": {"ok": True, "request_id": "r1", "status": "submitted"}})
    _inject_bridge(server, bridge)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/response", "POST",
        raw_body=_b64(json.dumps({
            "request_id": "r1",
            "response": {"response_type": "answered", "selected_option": "opt", "feedback": "f"},
        })),
    ))

    assert status == 200
    assert body == {"success": True}
    method, params, _ = bridge.calls[0]
    assert method == "respond"
    assert params["request_id"] == "r1"
    assert params["response"] == {
        "response_type": "answered",
        "selected_option": "opt",
        "answers": None,
        "feedback": "f",
    }


def test_proxy_cancel_tool_error_false(server: Any) -> None:
    """cancel 桥回 error → 取消转发失败 → success False。"""
    bridge = FakeBridge({"cancel": {"error": "request not found"}})
    _inject_bridge(server, bridge)

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/r1/cancel", "POST", raw_body=_b64("{}"),
    ))

    assert status == 200
    assert body == {"success": False, "request_id": "r1", "status": "cancelled"}


def test_proxy_viewed_via_bridge(server: Any) -> None:
    """viewed 端点经真实代理：确认应答（human sidecar 无 viewed 方法，不落库不转发）。"""
    bridge = FakeBridge()
    _inject_bridge(server, bridge)

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r1/viewed", "POST"))

    assert status == 200
    assert body == {"success": True, "request_id": "r1", "viewed": True}
    assert bridge.calls == []  # 无桥调用（确认应答）


def test_response_degraded_false(server: Any) -> None:
    server._get_human_interaction_service = lambda: None

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/response", "POST",
        raw_body=_b64(json.dumps({"request_id": "r1"})),
    ))

    assert status == 200
    assert body == {"success": False}


def test_cancel_degraded_false(server: Any) -> None:
    server._get_human_interaction_service = lambda: None

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r1/cancel", "POST"))

    assert status == 200
    assert body == {"success": False, "request_id": "r1", "status": "cancelled"}


def test_fallthrough_404_within_prefix(server: Any) -> None:
    """前缀内无匹配子路由（未知 action）→ 分派层 404（与命名空间外 404 区分）。"""
    _inject_service(server, FakeService())

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r1/unknown-action", "POST"))

    assert status == 404
    assert body["error"] == "not found"


def test_response_empty_body_400(server: Any) -> None:
    """空 body（无 request_id）→ 400（_decode_body 空串分支）。"""
    _inject_service(server, FakeService())

    status, body = _decode(_call(server, "/ext/approval_service/interaction/response", "POST"))

    assert status == 400
    assert body["detail"] == "缺少 request_id"


def test_viewed_with_degraded_service(server: Any) -> None:
    server._get_human_interaction_service = lambda: None

    status, body = _decode(_call(server, "/ext/approval_service/interaction/r1/viewed", "POST"))

    assert status == 200
    assert body == {"success": False, "request_id": "r1", "viewed": True}


def test_unknown_route_404(server: Any) -> None:
    status, body = _decode(_call(server, "/ext/approval_service/whatever"))

    assert status == 404


def test_invalid_json_body_400(server: Any) -> None:
    _inject_service(server, FakeService())

    status, body = _decode(_call(
        server, "/ext/approval_service/interaction/response", "POST", raw_body=_b64("{bad"),
    ))

    assert status == 400
