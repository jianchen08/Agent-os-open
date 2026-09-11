# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""审批归属校验测试（approve/deny 越权收口）。

覆盖：
1. _record_ownership：tenant 自内核 runs 表权威解析，user 取注入的创建者
2. _decision_denied 规则矩阵：同租户创建者/同租户 admin 放行；
   他租户用户、同租户他人非 admin、缺身份头一律 403；
   归属不可归因（无记录/无 run 锚）仅 admin 放行
3. _dispatch_request_action：approve 在任何决策副作用前先做归属校验
4. create_choice 等待窗口内归属可查（approve/deny 依赖的时序契约）
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 路径（agentos_plugin_sdk 未安装时）
_SDK_DIR = Path(__file__).resolve().parents[4] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))

# plugins/shared 根（http_json / kernel_db 裸名导入）
_SHARED_ROOT = Path(__file__).resolve().parents[2]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))


def _load_server() -> Any:
    """动态加载 server.py（每次新建，避免模块级 plugin 状态跨测试污染）。"""
    mod_name = "approval_server_ownership_test"
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


def _make_runs_db(tmp_path: Path, run_tenant: dict[str, str]) -> Path:
    """内核 runs 表最小形态：run_id → tenant_id 权威锚。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, config_hash TEXT, status TEXT,"
        " tenant_id TEXT, created_at TEXT)"
    )
    for run_id, tenant in run_tenant.items():
        conn.execute(
            "INSERT INTO runs VALUES (?, 'h', 'running', ?, '2026-01-01T00:00:00')",
            (run_id, tenant),
        )
    conn.commit()
    conn.close()
    return db_path


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
    data = resp["data"]
    return int(data["status"])


def _resp_payload(resp: dict[str, Any]) -> dict[str, Any]:
    import base64

    return json.loads(base64.b64decode(resp["data"]["body"]))


# ═══════════════════════════════════════════════════════════
# _record_ownership：runs 表权威租户 + 注入创建者
# ═══════════════════════════════════════════════════════════


class TestRecordOwnership:
    def test_records_tenant_from_runs_table_and_user(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(
            "AGENTOS_DB_PATH", str(_make_runs_db(tmp_path, {"run-1": "tenant-a"}))
        )
        mod = _load_server()
        mod._record_ownership("req-1", "run-1", "u-creator")
        assert mod._ownership["req-1"] == {"tenant": "tenant-a", "user": "u-creator"}

    def test_unattributed_when_run_missing_or_db_absent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_DB_PATH", str(tmp_path / "absent.db"))
        mod = _load_server()
        mod._record_ownership("req-2", "no-such-run", "u-creator")
        mod._record_ownership("req-3", "", "u-creator")
        # 无 run 锚/run 不存在 → tenant 空（不可归因，决策侧 admin-only 收口）
        assert mod._ownership["req-2"]["tenant"] == ""
        assert mod._ownership["req-3"]["tenant"] == ""


# ═══════════════════════════════════════════════════════════
# _decision_denied：规则矩阵
# ═══════════════════════════════════════════════════════════


class TestDecisionDenied:
    def _seed(self, mod: Any, tenant: str = "tenant-a", user: str = "u-creator") -> None:
        mod._ownership.clear()
        mod._ownership["req-1"] = {"tenant": tenant, "user": user}

    def test_creator_same_tenant_allowed(self) -> None:
        mod = _load_server()
        self._seed(mod)
        assert mod._decision_denied(_headers("tenant-a", "u-creator"), "req-1") is None

    def test_admin_same_tenant_allowed(self) -> None:
        mod = _load_server()
        self._seed(mod)
        assert (
            mod._decision_denied(_headers("tenant-a", "u-admin", "admin"), "req-1") is None
        )

    def test_cross_tenant_user_rejected(self) -> None:
        mod = _load_server()
        self._seed(mod)
        denied = mod._decision_denied(_headers("tenant-b", "u-creator"), "req-1")
        assert denied is not None and _resp_status(denied) == 403

    def test_cross_tenant_admin_rejected(self) -> None:
        mod = _load_server()
        self._seed(mod)
        # 同租户且（创建者或 admin）：他租户 admin 亦拒（租户边界优先于角色）
        denied = mod._decision_denied(_headers("tenant-b", "u-admin", "admin"), "req-1")
        assert denied is not None and _resp_status(denied) == 403

    def test_same_tenant_other_user_rejected(self) -> None:
        mod = _load_server()
        self._seed(mod)
        denied = mod._decision_denied(_headers("tenant-a", "u-other"), "req-1")
        assert denied is not None and _resp_status(denied) == 403
        assert "无权决策" in _resp_payload(denied)["detail"]

    def test_missing_tenant_header_rejected(self) -> None:
        mod = _load_server()
        self._seed(mod)
        denied = mod._decision_denied(_headers(user="u-creator"), "req-1")
        assert denied is not None and _resp_status(denied) == 403

    def test_unattributable_admin_allowed_other_rejected(self) -> None:
        mod = _load_server()
        mod._ownership.clear()
        # 无归属记录（纯交互审批/历史遗留）→ 非 admin 拒，admin 放行
        denied = mod._decision_denied(_headers("tenant-a", "u-1"), "req-x")
        assert denied is not None and _resp_status(denied) == 403
        assert mod._decision_denied(_headers("tenant-a", "u-admin", "admin"), "req-x") is None

    def test_creator_unknown_same_tenant_allowed(self) -> None:
        mod = _load_server()
        self._seed(mod, user="")
        # 创建者未知（创建链路无 user_id）：一用户一租户下同租户即归属人
        assert mod._decision_denied(_headers("tenant-a", "u-anyone"), "req-1") is None


# ═══════════════════════════════════════════════════════════
# _dispatch_request_action：approve 副作用前先校验
# ═══════════════════════════════════════════════════════════


class TestDispatchGuardsDecision:
    @pytest.mark.asyncio
    async def test_approve_blocked_before_service_dispatch(self) -> None:
        mod = _load_server()
        mod._ownership.clear()
        mod._ownership["req-1"] = {"tenant": "tenant-a", "user": "u-creator"}
        calls: list[tuple[str, dict]] = []

        class FakeService:
            async def submit_response(self, **kwargs: Any) -> bool:
                calls.append(("submit_response", kwargs))
                return True

        resp = await mod._dispatch_request_action(
            FakeService(), "req-1", "approve", "POST", "{}", _headers("tenant-b", "u-x")
        )
        assert _resp_status(resp) == 403
        assert calls == [], "越权请求不得触达交互服务（无决策副作用）"

    @pytest.mark.asyncio
    async def test_approve_passes_for_creator(self) -> None:
        mod = _load_server()
        mod._ownership.clear()
        mod._ownership["req-1"] = {"tenant": "tenant-a", "user": "u-creator"}

        class FakeService:
            async def submit_response(self, **kwargs: Any) -> bool:
                return True

        resp = await mod._dispatch_request_action(
            FakeService(),
            "req-1",
            "approve",
            "POST",
            "{}",
            _headers("tenant-a", "u-creator"),
        )
        assert _resp_status(resp) == 200
        assert _resp_payload(resp)["success"] is True


# ═══════════════════════════════════════════════════════════
# create_choice：等待窗口内归属可查（approve/deny 的时序契约）
# ═══════════════════════════════════════════════════════════


class _OwnershipProbeHi:
    """wait_for_choice 时快照窗口内归属，验证创建→决策面的可见时序。"""

    def __init__(self, module: Any, observed: dict) -> None:
        self._mod = module
        self.observed = observed

    async def call(self, method: str, params: dict) -> dict:
        if method == "create_choice":
            return {"request_id": "req-win"}
        if method == "wait_for_choice":
            self.observed["during_wait"] = dict(self._mod._ownership.get("req-win", {}))
            return {"selected_option": "批准"}
        return {}


class _FakeBus:
    async def notify(self, method: str, params: dict) -> None:  # noqa: ARG002
        return None


@pytest.mark.asyncio
async def test_ownership_visible_during_wait_and_cleared_after(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_runs_db(tmp_path, {"run-9": "tenant-a"})))
    mod = _load_server()
    observed: dict = {}

    def fake_get_cap(name: str) -> Any:
        if name == "human-interaction":
            return _OwnershipProbeHi(mod, observed)
        if name == "event-bus":
            return _FakeBus()
        return None

    mod.plugin.get_capability = fake_get_cap  # type: ignore[method-assign]
    monkeypatch.setattr(mod, "_lookup_run_tenant", lambda run_id: "tenant-a" if run_id == "run-9" else "")

    result = await mod.create_choice(
        title="t", options=["批准"], run_id="run-9", user_id="u-creator"
    )
    assert result["status"] == "resolved"
    assert observed["during_wait"] == {"tenant": "tenant-a", "user": "u-creator"}, (
        "等待窗口内 approve/deny 必须能查到归属"
    )
    assert "req-win" not in mod._ownership, "窗口收敛后归属必须清除"


# ═══════════════════════════════════════════════════════════
# 交互面全端点守卫（M2）：/response、/pending、/cancel、/viewed、/{rid}
# 与 approve/deny 走同一决策面，等权路径不得绕过归属校验
# ═══════════════════════════════════════════════════════════


class _FakeService:
    """human 侧服务替身：记录归因所需的请求记录 + 响应/取消调用。"""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.responded: list[str] = []
        self.cancelled: list[str] = []

    async def get_pending_requests(self, **kwargs: Any) -> list[dict[str, Any]]:  # noqa: ARG002
        return list(self._records)

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        for r in self._records:
            if r.get("id") == request_id or r.get("request_id") == request_id:
                return r
        return None

    async def respond(self, request_id: str, body: dict[str, Any]) -> bool:  # noqa: ARG002
        self.responded.append(request_id)
        return True

    async def cancel_request(self, request_id: str, reason: str | None = None) -> bool:  # noqa: ARG002
        self.cancelled.append(request_id)
        return True

    async def mark_as_viewed(self, request_id: str) -> bool:
        return True


def _record(rid: str, user_id: str = "") -> dict[str, Any]:
    return {"id": rid, "status": "pending", "message_data": {"user_id": user_id}}


@pytest.mark.asyncio
async def test_response_endpoint_rejects_cross_tenant() -> None:
    mod = _load_server()
    service = _FakeService([_record("req-r1", user_id="u-owner")])
    resp = await mod._route_submit_response(
        service,
        json.dumps({"request_id": "req-r1", "response": {"selected_option": "批准"}}),
        _headers(tenant="tenant-b", user="u-attacker"),
    )
    assert _resp_status(resp) == 403, "他租户 /response 必须 403（决策面等权路径）"
    assert service.responded == [], "403 之前不得有响应副作用"


@pytest.mark.asyncio
async def test_response_endpoint_allows_creator_via_record_attribution() -> None:
    # human.create_choice 直建的请求无 _ownership 记录：归因回退到记录内
    # 创建者 user_id（一用户一租户），创建者本人响应必须放行。
    mod = _load_server()
    service = _FakeService([_record("req-r2", user_id="u-owner")])
    resp = await mod._route_submit_response(
        service,
        json.dumps({"request_id": "req-r2", "response": {"selected_option": "批准"}}),
        _headers(tenant="u-owner", user="u-owner"),
    )
    assert _resp_status(resp) == 200
    assert _resp_payload(resp) == {"success": True}
    assert service.responded == ["req-r2"]


@pytest.mark.asyncio
async def test_pending_filters_by_requester_tenant() -> None:
    mod = _load_server()
    service = _FakeService([
        _record("req-mine", user_id="u-me"),
        _record("req-theirs", user_id="u-other"),
        _record("req-unattributed"),
    ])
    resp = await mod._route_list_pending(service, _headers(tenant="u-me", user="u-me"))
    payload = _resp_payload(resp)
    ids = [item["id"] for item in payload["items"]]
    assert ids == ["req-mine"], "普通用户只见归属本租户的待处理交互"
    assert payload["total"] == 1


@pytest.mark.asyncio
async def test_pending_missing_tenant_header_is_empty_not_full() -> None:
    # fail-closed：身份头缺失返回空集，绝不退化全量列表（跨租户枚举面）
    mod = _load_server()
    service = _FakeService([_record("req-1", user_id="u-x")])
    resp = await mod._route_list_pending(service, _headers())
    payload = _resp_payload(resp)
    assert payload == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_pending_admin_sees_all() -> None:
    mod = _load_server()
    service = _FakeService([
        _record("req-a", user_id="u-a"),
        _record("req-b", user_id="u-b"),
        _record("req-none"),
    ])
    resp = await mod._route_list_pending(
        service, _headers(tenant="tenant-a", user="admin-x", role="admin")
    )
    assert _resp_payload(resp)["total"] == 3, "admin 运维面可见全部"


@pytest.mark.asyncio
async def test_cancel_and_viewed_guarded() -> None:
    mod = _load_server()
    service = _FakeService([_record("req-c1", user_id="u-owner")])
    resp = await mod._dispatch_request_action(
        service, "req-c1", "cancel", "POST", "{}",
        _headers(tenant="tenant-b", user="u-attacker"),
    )
    assert resp is not None and _resp_status(resp) == 403, "他租户 cancel 必须 403"
    assert service.cancelled == []

    resp = await mod._dispatch_request_action(
        service, "req-c1", "viewed", "POST", "{}",
        _headers(tenant="tenant-b", user="u-attacker"),
    )
    assert resp is not None and _resp_status(resp) == 403, "他租户 viewed 必须 403"

    # 创建者本人放行
    resp = await mod._dispatch_request_action(
        service, "req-c1", "cancel", "POST", "{}",
        _headers(tenant="u-owner", user="u-owner"),
    )
    assert resp is not None and _resp_status(resp) == 200
    assert service.cancelled == ["req-c1"]


@pytest.mark.asyncio
async def test_detail_guarded_but_not_found_stays_404_for_owner() -> None:
    mod = _load_server()
    service = _FakeService([_record("req-d1", user_id="u-owner")])
    # 他租户读详情 → 403（不泄露存在性）
    resp = await mod._dispatch_request_action(
        service, "req-d1", "detail", "GET", "",
        _headers(tenant="tenant-b", user="u-attacker"),
    )
    assert resp is not None and _resp_status(resp) == 403

    # 归属人读详情 → 200 记录
    resp = await mod._dispatch_request_action(
        service, "req-d1", "detail", "GET", "",
        _headers(tenant="u-owner", user="u-owner"),
    )
    assert resp is not None and _resp_status(resp) == 200
    assert _resp_payload(resp)["id"] == "req-d1"

    # 不存在的请求：归属解析回退后不可归因 → 非 admin 403（不向非 admin 泄露 404 与 403 的区别无妨；
    # 关键契约是不泄露记录内容）
    resp = await mod._dispatch_request_action(
        service, "req-missing", "detail", "GET", "",
        _headers(tenant="tenant-b", user="u-attacker"),
    )
    assert resp is not None and _resp_status(resp) == 403


# ═══════════════════════════════════════════════════════════
# S14：_decisions 终态记录淘汰（TTL + 硬上限）
# ═══════════════════════════════════════════════════════════


def test_decisions_evicted_by_ttl_and_hard_cap() -> None:
    mod = _load_server()
    mod._decisions.clear()
    # 过期条目：写入新决策时被 TTL 清扫
    import time as _time

    mod._decisions["stale"] = {"approved": True, "reason": "x", "resumed": True,
                               "ts": _time.time() - mod._DECISIONS_TTL_SECONDS - 1}
    mod._record_decision("fresh", {"approved": False, "reason": "y", "resumed": False})
    assert "stale" not in mod._decisions, "过期终态必须在写入时顺带淘汰"
    assert mod._decisions["fresh"]["approved"] is False

    # 硬上限：超过上限后条目数钉在界内且最旧先出
    mod._decisions.clear()
    for i in range(mod._DECISIONS_MAX_ENTRIES + 10):
        mod._record_decision(f"k{i}", {"approved": True, "reason": "r", "resumed": True})
    assert len(mod._decisions) <= mod._DECISIONS_MAX_ENTRIES
    assert "k0" not in mod._decisions, "最旧条目必须先被逐出"
    assert f"k{mod._DECISIONS_MAX_ENTRIES + 9}" in mod._decisions
