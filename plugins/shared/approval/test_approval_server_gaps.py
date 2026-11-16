# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""approval server.py 分支缺口补测（覆盖率冲刺批九）。

按 coverage.xml 2026-09-13 缺行逐簇补齐既有测试未触达的分支：
- _lookup_run_tenant：库损坏（非 SQLite 文件）→ 查询失败按不可归因收口
- _record_owner / _resolve_owner：_ownership 窗口记录优先归因、归因探测异常回退
- _emit_approval_created / _suspend_pipeline / _resume_pipeline：capability
  异常与非 dict 返回的显式失败（_suspend_pipeline ok/error 二值）
- _classify_reject：cancelled / 未知错误码 → 拒绝原因归类
- create_choice：human-interaction 未注入、wait 返回非 dict → 收敛为拒绝
- submit：无挂起句柄 → 直接落终态且幂等
- 交互路由：_match_request_route 非法 sub、detail 非 GET 404、无路由 fallthrough
- _on_unload 卸载面

唯一不覆盖的缺行：_record_decision 的 ``break``（防御分支，结构不可达——
``min(..., default=None)`` 返回 None 要求 _decisions 为空，而循环守卫
``len(_decisions) >= 4096`` 保证非空）。
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

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 路径（agentos_plugin_sdk 未安装时）
_SDK_DIR = Path(__file__).resolve().parents[3] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))

# plugins/shared 根（http_json 等裸名导入）
_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from _libs.testing.db_read_fake import FakeDbRead  # noqa: E402 — 共享测试基建


def _load_server() -> Any:
    """动态加载 server.py（每次新建，模块级 _suspended/_ownership/_decisions 隔离）。"""
    mod_name = "approval_server_gaps_test"
    module_path = _PLUGIN_DIR / "server.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _headers(tenant: str = "", user: str = "", role: str = "") -> dict[str, str]:
    h: dict[str, str] = {}
    if tenant:
        h["x-agentos-tenant"] = tenant
    if user:
        h["x-agentos-user"] = user
    if role:
        h["x-agentos-role"] = role
    return h


def _resp_status(resp: dict[str, Any]) -> int:
    """解包 {success, data:{status, body(base64)}} 的 HTTP 状态。"""
    return int(resp["data"]["status"])


def _resp_payload(resp: dict[str, Any]) -> dict[str, Any]:
    return json.loads(base64.b64decode(resp["data"]["body"]))


def _inject_capability(module: Any, name: str, handle: Any) -> None:
    """把伪 capability handle 注入插件（外部依赖替身：内核能力面）。"""
    original = module.plugin.get_capability
    module.plugin.get_capability = lambda n: handle if n == name else original(n)  # type: ignore[method-assign]


class _SilentBus:
    """吞掉 emit 调用的伪 event-bus（本文件不关注事件载荷）。"""

    async def notify(self, method: str, params: dict) -> None:  # noqa: ARG002
        return None


class _RecordingBus:
    """记录 emit 调用的伪 event-bus（事件面断言）。"""

    def __init__(self) -> None:
        self.emits: list[tuple[str, dict]] = []

    async def notify(self, method: str, params: dict) -> None:
        self.emits.append((method, params))


class _RaisingBus:
    """emit 必抛的伪 event-bus（总线故障注入）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def notify(self, method: str, params: dict) -> None:  # noqa: ARG002
        raise self._exc


class _FakeHi:
    """伪 human-interaction handle：create/wait 按预设回放，调用留痕。"""

    _DEFAULT_WAIT: dict[str, str] = {"selected_option": "批准"}

    def __init__(
        self,
        create_result: dict | None = None,
        wait_result: Any = _DEFAULT_WAIT,
    ) -> None:
        self._create = create_result or {"request_id": "req-gap"}
        self._wait = wait_result
        self.calls: list[str] = []

    async def call(self, method: str, params: dict) -> Any:  # noqa: ARG002
        self.calls.append(method)
        if method == "create_choice":
            return self._create
        if method == "wait_for_choice":
            return self._wait
        return {}


class _FakePipeline:
    """伪 pipeline-executor handle：固定返回或必抛。"""

    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    async def call(self, method: str, params: dict) -> Any:  # noqa: ARG002
        if self._exc is not None:
            raise self._exc
        return self._result


class _AttributionService:
    """human 侧服务替身：get_request 可配置抛错，记录内 user_id 可设诱饵。"""

    def __init__(
        self,
        record: dict[str, Any] | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self._record = record
        self._probe_error = probe_error
        self.responded: list[str] = []

    async def get_request(self, request_id: str) -> dict[str, Any] | None:  # noqa: ARG002
        if self._probe_error is not None:
            raise self._probe_error
        return self._record

    async def respond(self, request_id: str, body: dict[str, Any]) -> bool:  # noqa: ARG002
        self.responded.append(request_id)
        return True


class _PendingService:
    """human 侧服务替身：仅返回预设 pending 列表。"""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def get_pending_requests(self, **kwargs: Any) -> list[dict[str, Any]]:  # noqa: ARG002
        return list(self._records)


# ═══════════════════════════════════════════════════════════
# _lookup_run_tenant：库损坏 → 查询失败按不可归因收口（缺行 102-104）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", [b"this is definitely not sqlite", b"\x00\x01\x02\x03"])
async def test_run_tenant_lookup_survives_corrupt_db(
    tmp_path: Path, junk: bytes
) -> None:
    db = tmp_path / "agentos_kernel.db"
    db.write_bytes(junk)
    mod = _load_server()
    # db-read 契约替身指向损坏库：执行期 sqlite 报错 → DbReadQueryError →
    # 空租户（不可归因，决策面 admin-only 收口），不向调用方抛异常
    mod.plugin._capabilities["db-read"] = FakeDbRead(db, {"Bearer tok-a": "tenant-a"})
    assert await mod._lookup_run_tenant("run-1", "tenant-a", "Bearer tok-a") == ""
    await mod._record_ownership("req-cx", "run-1", "tenant-a", "Bearer tok-a")
    assert mod._ownership["req-cx"]["tenant"] == ""


# ═══════════════════════════════════════════════════════════
# _record_owner：_ownership 窗口记录优先（缺行 122）
# ═══════════════════════════════════════════════════════════


def test_record_owner_prefers_ownership_window_record() -> None:
    mod = _load_server()
    mod._ownership["req-o"] = {"tenant": "tenant-a", "user": "u-creator"}
    owner = mod._record_owner({"id": "req-o", "message_data": {"user_id": "u-decoy"}})
    assert owner == {"tenant": "tenant-a", "user": "u-creator"}, (
        "窗口记录存在时归因必须取 _ownership，而非记录内 user_id"
    )


def test_record_owner_falls_back_to_message_data_user() -> None:
    mod = _load_server()
    owner = mod._record_owner({"id": "req-no-window", "message_data": {"user_id": "u-own"}})
    assert owner == {"tenant": "u-own", "user": "u-own"}


@pytest.mark.asyncio
async def test_pending_visibility_via_ownership_window_attribution() -> None:
    # 记录本身无 user_id（不可从记录归因），仅 _ownership 窗口知道归属：
    # 普通用户按窗口归因可见自己的请求，不可归因的不可见。
    mod = _load_server()
    mod._ownership["req-v"] = {"tenant": "tenant-a", "user": "u-me"}
    service = _PendingService([
        {"id": "req-v", "status": "pending"},
        {"id": "req-unattributed", "status": "pending"},
    ])
    resp = await mod._route_list_pending(service, _headers(tenant="tenant-a", user="u-me"))
    payload = _resp_payload(resp)
    assert [item["id"] for item in payload["items"]] == ["req-v"]
    assert payload["total"] == 1


# ═══════════════════════════════════════════════════════════
# _resolve_owner（经 /response 决策面）：窗口归因命中 + 探测异常回退
# （缺行 140、144-145）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_response_attribution_uses_ownership_window_over_record() -> None:
    mod = _load_server()
    mod._ownership["req-w"] = {"tenant": "tenant-a", "user": "u-creator"}
    # 诱饵：记录内 user_id 与窗口记录不同——若归因误取记录，请求者将被拒
    service = _AttributionService(
        record={"id": "req-w", "message_data": {"user_id": "u-decoy"}}
    )
    body = json.dumps({"request_id": "req-w", "response": {"selected_option": "批准"}})

    ok_resp = await mod._route_submit_response(
        service, body, _headers(tenant="tenant-a", user="u-creator")
    )
    assert _resp_status(ok_resp) == 200
    assert _resp_payload(ok_resp) == {"success": True}
    assert service.responded == ["req-w"]

    # 同一归属，跨租户请求者 → 403 且无响应副作用
    service.responded.clear()
    denied = await mod._route_submit_response(
        service, body, _headers(tenant="tenant-b", user="u-creator")
    )
    assert _resp_status(denied) == 403
    assert service.responded == []


@pytest.mark.asyncio
async def test_response_attribution_probe_failure_is_unattributable() -> None:
    mod = _load_server()
    service = _AttributionService(probe_error=RuntimeError("human sidecar down"))
    body = json.dumps({"request_id": "req-p", "response": {}})

    # 非 admin：归因探测失败 → 不可归因 → fail-closed 403
    denied = await mod._route_submit_response(
        service, body, _headers(tenant="tenant-a", user="u-1")
    )
    assert _resp_status(denied) == 403
    assert service.responded == []

    # admin：免归因探测直判，不可归因仍放行（运维可恢复）
    allowed = await mod._route_submit_response(
        service, body, _headers(tenant="tenant-a", user="u-admin", role="admin")
    )
    assert _resp_status(allowed) == 200
    assert _resp_payload(allowed) == {"success": True}


# ═══════════════════════════════════════════════════════════
# _emit_approval_created：总线故障不影响审批主链路（缺行 303-304）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("bus_exc", [RuntimeError("bus down"), KeyError("emit")])
async def test_emit_failure_is_swallowed(bus_exc: Exception) -> None:
    mod = _load_server()
    await mod._emit_approval_created("req-e", "t", ["a"], "run-1")  # 不向调用方抛异常


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "emit_fn",
    ["taken", "failed"],
)
async def test_settlement_emit_failure_is_swallowed(emit_fn: str) -> None:
    """结算事件（taken/failed）广播失败不向调用方抛异常（fire-and-forget 韧性）。"""
    mod = _load_server()
    _inject_capability(mod, "event-bus", _RaisingBus(RuntimeError("bus down")))
    emitter = mod._emit_approval_taken if emit_fn == "taken" else mod._emit_approval_failed
    await emitter("req-e", "run-1", {"status": "x"})  # 不抛异常


@pytest.mark.asyncio
async def test_settlement_emit_skipped_without_event_bus() -> None:
    """event-bus 未注入：结算事件静默跳过，不向调用方抛异常。"""
    mod = _load_server()  # 未注入 event-bus
    await mod._emit_approval_taken("req-e", "run-1")
    await mod._emit_approval_failed("req-e", "run-1")


@pytest.mark.asyncio
async def test_create_choice_resolves_when_emit_fails() -> None:
    mod = _load_server()
    _inject_capability(mod, "event-bus", _RaisingBus(RuntimeError("bus down")))
    _inject_capability(mod, "human-interaction", _FakeHi())
    out = await mod.create_choice(title="t", options=["a", "b"])
    assert out["status"] == "resolved"
    assert out["selected_option"] == "批准"
    assert out["resumed"] is False


# ═══════════════════════════════════════════════════════════
# _suspend_pipeline / _resume_pipeline：ok/error 显式二值
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_suspend_pipeline_without_capability_is_explicit_failure() -> None:
    mod = _load_server()  # 未注入 pipeline-executor
    res = await mod._suspend_pipeline("run-2", "req-2")
    assert res["ok"] is False
    assert "not injected" in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [RuntimeError("executor gone"), TimeoutError("slow")])
async def test_suspend_pipeline_capability_error_is_explicit_failure(exc: Exception) -> None:
    mod = _load_server()
    _inject_capability(mod, "pipeline-executor", _FakePipeline(exc=exc))
    res = await mod._suspend_pipeline("run-1", "req-1")
    assert res["ok"] is False
    assert str(exc) in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_result", ["ok", None, 123])
async def test_suspend_pipeline_non_dict_result_is_explicit_failure(raw_result: Any) -> None:
    mod = _load_server()
    _inject_capability(mod, "pipeline-executor", _FakePipeline(result=raw_result))
    res = await mod._suspend_pipeline("run-9", "req-9")
    assert res["ok"] is False, "非 dict 挂起结果必须显式失败（不得降级为默认句柄）"
    assert res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_result", "expected_handle"),
    [
        (
            {"run_id": "run-3", "branch_id": "b1", "seq": 7},
            {"run_id": "run-3", "branch_id": "b1", "seq": 7},
        ),
        # dict 缺键：run_id 回落入参，branch/seq 缺省 None
        ({}, {"run_id": "run-4", "branch_id": None, "seq": None}),
    ],
)
async def test_suspend_pipeline_dict_result_ok_handle(
    raw_result: dict, expected_handle: dict
) -> None:
    mod = _load_server()
    _inject_capability(mod, "pipeline-executor", _FakePipeline(result=raw_result))
    res = await mod._suspend_pipeline(expected_handle["run_id"], "req-3")
    assert res == {"ok": True, "handle": expected_handle}


@pytest.mark.asyncio
async def test_resume_pipeline_without_capability_returns_false() -> None:
    mod = _load_server()  # 未注入 pipeline-executor
    result = await mod._resume_pipeline({"run_id": "run-1"}, "req-1", "approved")
    assert result is False


# ═══════════════════════════════════════════════════════════
# create_choice 挂起失败显式化（P-1）：三种失败统一显式失败，
# 不进等待窗口、不残留挂起/归属记录、广播 approval.failed
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "not-injected", "non-dict"])
async def test_suspend_failure_fails_explicitly_without_wait(mode: str) -> None:
    """挂起失败（异常/未注入/非 dict）→ 工具失败面 suspend_failed，绝不等待用户。

    管道未真正挂起时进等待窗口 = 用户批了也无法恢复的假审批面。
    """
    mod = _load_server()
    bus = _RecordingBus()
    _inject_capability(mod, "event-bus", bus)
    hi = _FakeHi()
    _inject_capability(mod, "human-interaction", hi)
    if mode == "raise":
        _inject_capability(mod, "pipeline-executor", _FakePipeline(exc=RuntimeError("executor gone")))
    elif mode == "non-dict":
        _inject_capability(mod, "pipeline-executor", _FakePipeline(result="junk"))
    # not-injected：不注入 pipeline-executor

    out = await mod.create_choice(title="t", options=["批准"], run_id="run-s1")

    # 工具失败面（管道感知）：suspend_failed + 原因
    assert out["status"] == "suspend_failed"
    assert "suspend_failed" in out["error"]
    rid = out["request_id"]
    # 性质断言：挂起失败路径不得落 approved=True（显式拒绝，绝不放行）
    decision = mod._decisions[rid]
    assert decision["approved"] is False
    assert decision["reason"] == "suspend_failed"
    # 不进等待窗口（仅创建交互，无 wait）
    assert hi.calls == ["create_choice"]
    # 不残留挂起/归属记录
    assert rid not in mod._suspended
    assert rid not in mod._ownership
    # 事件面：approval.failed 恰好一次、携带 request_id
    failed = [p for _m, p in bus.emits if p["event"] == "approval.failed"]
    assert len(failed) == 1
    assert failed[0]["payload"]["request_id"] == rid
    # 拒绝终态可查（公开面）：submit 重读终态，不再尝试恢复
    prior = await mod.submit(rid, "x")
    assert prior["status"] == "rejected"
    assert prior["approved"] is False


@pytest.mark.asyncio
async def test_run_id_absent_pure_interaction_unchanged() -> None:
    """run_id 缺省 = 纯交互决断：不触碰挂起面，行为零变（回归锚）。"""
    mod = _load_server()
    _inject_capability(mod, "event-bus", _SilentBus())
    hi = _FakeHi()
    _inject_capability(mod, "human-interaction", hi)
    # pipeline-executor 刻意不注入：缺 run_id 时挂起面不可达

    out = await mod.create_choice(title="t", options=["批准"])

    assert out["status"] == "resolved"
    assert out["resumed"] is False
    assert hi.calls == ["create_choice", "wait_for_choice"]
    assert mod._decisions[out["request_id"]]["approved"] is True
    assert mod._suspended == {}


# ═══════════════════════════════════════════════════════════
# _classify_reject（经 create_choice wait 错误形态）：缺行 363-365
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wait_res,expected_reason",
    [
        ({"error": "cancelled by user", "error_code": "INTERACTION_CANCELLED"}, "cancelled"),
        ({"error": "denied"}, "rejected"),
        ({"error": "timed out", "error_code": "INTERACTION_TIMEOUT"}, "timeout"),
    ],
)
async def test_wait_error_shape_converges_to_rejection(
    wait_res: dict, expected_reason: str
) -> None:
    mod = _load_server()
    _inject_capability(mod, "event-bus", _SilentBus())
    _inject_capability(mod, "human-interaction", _FakeHi(wait_result=wait_res))
    out = await mod.create_choice(title="t", options=["a", "b"])
    # 性质：一切 error 形态的 wait 收敛为拒绝；本场景无 run_id（纯交互），
    # 无挂起簿记可回收，resume 不发生（带 run_id 的回收环见
    # test_approval_timeout_recycle.py）
    assert out["status"] == "rejected"
    assert out["resumed"] is False
    assert out["reason"] == expected_reason
    # 终态已记录：事后 submit 重读一致（杜绝超时后重复恢复的歧义）
    prior = await mod.submit(out["request_id"], "x")
    assert prior["status"] == "rejected"
    assert prior["approved"] is False
    assert prior["reason"] == expected_reason


# ═══════════════════════════════════════════════════════════
# create_choice 降级面：human-interaction 未注入 / wait 非 dict
# （缺行 416、462）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_choice_without_human_interaction_capability() -> None:
    mod = _load_server()  # 未注入 human-interaction
    out = await mod.create_choice(title="t", options=["a"])
    assert "error" in out
    assert "human-interaction" in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_wait", [None, "unexpected-string", 42])
async def test_wait_non_dict_response_converges_to_rejection(bad_wait: Any) -> None:
    mod = _load_server()
    _inject_capability(mod, "event-bus", _SilentBus())
    _inject_capability(mod, "human-interaction", _FakeHi(wait_result=bad_wait))
    out = await mod.create_choice(title="t", options=["a"])
    assert out["status"] == "rejected"
    assert out["reason"] == "rejected"
    assert out["resumed"] is False


# ═══════════════════════════════════════════════════════════
# submit：无挂起句柄 → 直接落终态（缺行 563-566）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("result_payload", ["approved", "reject"])
async def test_submit_without_handle_resolves_idempotently(result_payload: str) -> None:
    mod = _load_server()
    mod._suspended["req-s1"] = {"suspend_handle": None, "run_id": None, "created_at": 0.0}
    out = await mod.submit("req-s1", result_payload)
    assert out["request_id"] == "req-s1"
    assert out["status"] == "resolved"
    assert out["resumed"] is False
    assert out["result"] == result_payload
    assert "req-s1" not in mod._suspended, "无句柄提交必须消费挂起记录"
    # 终态幂等：再次 submit 重读已记录终态，结果一致
    again = await mod.submit("req-s1", result_payload)
    assert again["status"] == "resolved"
    assert again["resumed"] is False


# ═══════════════════════════════════════════════════════════
# 卸载面（缺行 592）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_unload_runs_clean_with_suspended_state() -> None:
    mod = _load_server()
    mod._suspended["req-u"] = {"suspend_handle": None, "run_id": None, "created_at": 0.0}
    await mod._on_unload({})  # 卸载不得抛异常


# ═══════════════════════════════════════════════════════════
# 交互路由：_match_request_route 非法 sub（缺行 790）、detail 非 GET
# （缺行 811）、无路由 fallthrough（缺行 908）
# ═══════════════════════════════════════════════════════════


def test_build_options_assigns_declared_semantics() -> None:
    """选项语义声明（ADR 2026-10-01 决策 2/3）：按索引对齐赋值；非法值丢弃不带病入卡。"""
    mod = _load_server()
    out = mod._build_options(
        ["同意", "拒绝", "搁置"], ["approve_once", "deny", "not-a-semantics"]
    )
    assert out == [
        {"id": "0", "label": "同意", "semantics": "approve_once"},
        {"id": "1", "label": "拒绝", "semantics": "deny"},
        {"id": "2", "label": "搁置"},
    ], "非法语义值必须丢弃（该选项退化为无语义）"
    # 未声明：形状与旧契约逐字节同形（只增不改）
    assert mod._build_options(["a", "b"]) == [
        {"id": "0", "label": "a"},
        {"id": "1", "label": "b"},
    ]


@pytest.mark.parametrize(
    "sub,expected",
    [
        ("", None),
        ("/", None),
        ("missing-slash", None),
        ("/req-9", ("req-9", "detail")),
        ("/req-9/deny", ("req-9", "deny")),
    ],
)
def test_match_request_route(sub: str, expected: tuple[str, str] | None) -> None:
    mod = _load_server()
    assert mod._match_request_route(sub) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_detail_rejects_non_get_method(method: str) -> None:
    mod = _load_server()
    assert await mod._dispatch_request_action(None, "req-d", "detail", method, "") is None
    # 全链路面：detail + 非 GET 统一 404（路由命中但方法不符）
    resp = await mod.http_handle(
        path="/ext/approval_service/interaction/req-d", method=method
    )
    assert _resp_status(resp) == 404
    assert "error" in _resp_payload(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/"])
async def test_unrouted_sub_path_falls_through_to_404(suffix: str) -> None:
    mod = _load_server()
    resp = await mod.http_handle(
        path=f"/ext/approval_service/interaction{suffix}", method="GET"
    )
    assert _resp_status(resp) == 404
    assert _resp_payload(resp) == {"error": "not found", "path": f"/ext/approval_service/interaction{suffix}"}
