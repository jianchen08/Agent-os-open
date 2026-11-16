# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""审批归属校验测试（approve/deny 越权收口）。

覆盖：
1. _record_ownership：tenant = 创建者 user_id（一用户一租户），经 db-read
   契约（pipeline_state tenant 档，身份经 authorization 穿线）run_id 锚验证；
   user 取注入的创建者
2. _decision_denied 规则矩阵：同租户创建者/同租户 admin 放行；
   他租户用户、同租户他人非 admin、缺身份头一律 403；
   归属不可归因（无记录/无 run 锚）仅 admin 放行
3. _dispatch_request_action：approve 在任何决策副作用前先做归属校验
4. create_choice 等待窗口内归属可查（approve/deny 依赖的时序契约）；管道步
   上下文无凭证时 run 锚降级、创建者归属经 human 记录归因回退兜底
5. F8 租户边界显式化：run_id 反查走 db-read 契约（收窄内建）——他租户同值
   run_id 不可达，来源字段不串租户拼装
"""

from __future__ import annotations

import asyncio
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
_SDK_DIR = Path(__file__).resolve().parents[3] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))

# plugins/shared 根（http_json / db_read / _libs 测试替身裸名导入）
_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from _libs.testing.db_read_fake import FakeDbRead  # noqa: E402 — 共享测试基建


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
    """内核 pipeline_state 最小形态：run_id 标量键 → tenant_id 权威锚（ADR 2026-09-18）。"""
    return _make_state_db(
        tmp_path,
        [(f"pipe-{i}", tenant, {"run_id": run_id}) for i, (run_id, tenant) in enumerate(run_tenant.items())],
    )


def _make_state_db(tmp_path: Path, rows: list[tuple[str, str, dict[str, str]]]) -> Path:
    """内核 pipeline_state 最小形态：按 (pipeline_id, tenant_id) 播种标量键行。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT,"
        " value_kind TEXT DEFAULT 'str', tenant_id TEXT, updated_at TEXT,"
        " PRIMARY KEY (pipeline_id, field_key, tenant_id))"
    )
    for pipeline_id, tenant, fields in rows:
        for k, v in fields.items():
            conn.execute(
                "INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at)"
                " VALUES (?, ?, ?, 'str', ?, '2026-01-01T00:00:00')",
                (pipeline_id, k, v, tenant),
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
# _record_ownership：创建者租户 + 注入创建者（run 锚租户内验证）
# ═══════════════════════════════════════════════════════════

# db-read 身份链测试令牌（token → 租户，仿内核复权后的收窄值）
_TOK_A = "Bearer tok-a"
_TOK_B = "Bearer tok-b"


def _install_db_read(mod: Any, db_path: Path, corrupt: bool = False) -> FakeDbRead:
    """db-read capability 替身注入（归属锚查询的唯一数据通道）。"""
    if corrupt:
        db_path.write_bytes(b"this is definitely not sqlite" * 8)
    fake = FakeDbRead(db_path, {_TOK_A: "tenant-a", _TOK_B: "tenant-b"})
    mod.plugin._capabilities["db-read"] = fake
    return fake


class TestRecordOwnership:
    @pytest.mark.asyncio
    async def test_records_tenant_from_creator_user_verified_by_run_anchor(
        self, tmp_path: Path
    ) -> None:
        db = _make_runs_db(tmp_path, {"run-1": "tenant-a"})
        mod = _load_server()
        fake = _install_db_read(mod, db)
        # 一用户一租户：归属租户 = 创建者 user_id；run 锚在租户内在位才归因。
        # 身份经 authorization 穿线（db-read 契约复权），tenant_id 形参为
        # fail-closed 闸
        await mod._record_ownership("req-1", "run-1", "tenant-a", _TOK_A)
        assert mod._ownership["req-1"] == {"tenant": "tenant-a", "user": "tenant-a"}
        assert fake.calls[0]["face"] == "pipeline_state"
        assert fake.calls[0]["_authorization"] == _TOK_A

    @pytest.mark.asyncio
    async def test_unattributed_when_run_missing_or_db_absent(self, tmp_path: Path) -> None:
        mod = _load_server()
        _install_db_read(mod, tmp_path / "absent.db")
        await mod._record_ownership("req-2", "no-such-run", "tenant-a", _TOK_A)
        await mod._record_ownership("req-3", "", "tenant-a", _TOK_A)
        # 无 run 锚/run 不存在 → tenant 空（不可归因，决策侧 admin-only 收口）
        assert mod._ownership["req-2"]["tenant"] == ""
        assert mod._ownership["req-3"]["tenant"] == ""

    @pytest.mark.asyncio
    async def test_unattributed_when_creator_missing_even_if_run_exists(
        self, tmp_path: Path
    ) -> None:
        # 创建者缺失 = 无租户闸可过 → 不查询直接不可归因（fail-closed）
        db = _make_runs_db(tmp_path, {"run-1": "tenant-a"})
        mod = _load_server()
        fake = _install_db_read(mod, db)
        await mod._record_ownership("req-4", "run-1", "", _TOK_A)
        assert mod._ownership["req-4"] == {"tenant": "", "user": ""}
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_unattributed_when_identity_missing_fails_closed(
        self, tmp_path: Path
    ) -> None:
        # 契约身份缺失（无凭证 → 内核 403 信封）：不可归因降级，绝不退化全租户
        db = _make_runs_db(tmp_path, {"run-1": "tenant-a"})
        mod = _load_server()
        _install_db_read(mod, db)
        await mod._record_ownership("req-5", "run-1", "tenant-a", "")
        assert mod._ownership["req-5"]["tenant"] == ""


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
            async def get_request(self, rid: str) -> dict[str, Any]:
                # approve 查创建方 options 中 approve_once 语义的 id（ADR 2026-10-01）
                return {
                    "id": rid,
                    "message_data": {
                        "options": [{"id": "0", "label": "同意", "semantics": "approve_once"}]
                    },
                }

            async def submit_response(self, **kwargs: Any) -> bool:
                FakeService.submitted = kwargs
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
        # 路由不自造词形（ADR 2026-10-01 决策 3）：按 approve_once 语义查 id 提交，
        # 不固定提交 approved/approve 词形。
        assert FakeService.submitted["response_type"] == "answered"
        assert FakeService.submitted["selected_option"] == "0"

    @pytest.mark.asyncio
    async def test_approve_without_declared_semantics_fails_closed(self) -> None:
        """卡面未声明 approve_once 语义 → 422 显式失败，无决策副作用（fail-closed）。"""
        mod = _load_server()
        mod._ownership.clear()
        mod._ownership["req-2"] = {"tenant": "tenant-a", "user": "u-creator"}
        calls: list[tuple[str, dict]] = []

        class FakeService:
            async def get_request(self, rid: str) -> dict[str, Any]:
                return {
                    "id": rid,
                    "message_data": {
                        "options": [{"id": "0", "label": "同意"}]  # 无语义声明
                    },
                }

            async def submit_response(self, **kwargs: Any) -> bool:  # noqa: ARG002
                calls.append(("submit_response", kwargs))
                return True

        resp = await mod._dispatch_request_action(
            FakeService(),
            "req-2",
            "approve",
            "POST",
            "{}",
            _headers("tenant-a", "u-creator"),
        )
        assert _resp_status(resp) == 422
        assert _resp_payload(resp)["success"] is False
        assert calls == [], "未声明语义不得代批（无副作用）"

    @pytest.mark.asyncio
    async def test_denies_via_response_type_without_word_form(self) -> None:
        """deny 路由零词形生产：response_type=denied 即显式拒绝，不提交 reject 词形。"""
        mod = _load_server()
        mod._ownership.clear()
        mod._ownership["req-3"] = {"tenant": "tenant-a", "user": "u-creator"}
        calls: list[tuple[str, dict]] = []

        class FakeService:
            async def get_request(self, rid: str) -> dict[str, Any]:  # noqa: ARG002
                return {"id": rid, "message_data": {"options": []}}

            async def submit_response(self, **kwargs: Any) -> bool:
                calls.append(("submit_response", kwargs))
                return True

        resp = await mod._dispatch_request_action(
            FakeService(),
            "req-3",
            "deny",
            "POST",
            "{}",
            _headers("tenant-a", "u-creator"),
        )
        assert _resp_status(resp) == 200
        assert _resp_payload(resp)["success"] is True
        assert calls, "deny 必须提交显式拒绝"
        assert calls[0][1]["response_type"] == "denied"
        assert calls[0][1]["selected_option"] is None, "拒绝路由不再自造 reject 词形"


# ═══════════════════════════════════════════════════════════
# create_choice：等待窗口内归属可查（approve/deny 的时序契约）
# ═══════════════════════════════════════════════════════════


class _OwnershipProbeHi:
    """wait_for_choice 时快照窗口内归属，验证创建→决策面的可见时序。

    同时扮演 human 侧请求记录源：create_choice 透传的 user_id 落入记录
    （服务端注入链），供 _resolve_owner 的记录归因回退消费。
    """

    def __init__(self, module: Any, observed: dict) -> None:
        self._mod = module
        self.observed = observed
        self.records: dict[str, dict] = {}

    async def call(self, method: str, params: dict) -> dict:
        if method == "create_choice":
            self.records["req-win"] = {"message_data": {"user_id": params.get("user_id", "")}}
            return {"request_id": "req-win"}
        if method == "wait_for_choice":
            self.observed["during_wait"] = dict(self._mod._ownership.get("req-win", {}))
            return {"selected_option": "批准"}
        return {}

    async def get_request(self, request_id: str) -> dict:
        return self.records.get(request_id, {})


class _FakeBus:
    async def notify(self, method: str, params: dict) -> None:  # noqa: ARG002
        return None


class _FakePipeline:
    """伪 pipeline-executor：suspend/resume 正常返回（挂起失败会中止等待窗口）。"""

    async def call(self, method: str, params: dict) -> dict:  # noqa: ARG002
        return {"run_id": params.get("run_id"), "branch_id": "b0", "seq": 1}


@pytest.mark.asyncio
async def test_ownership_visible_during_wait_and_cleared_after(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_server()
    observed: dict = {}
    hi = _OwnershipProbeHi(mod, observed)

    def fake_get_cap(name: str) -> Any:
        if name == "human-interaction":
            return hi
        if name == "event-bus":
            return _FakeBus()
        if name == "pipeline-executor":
            return _FakePipeline()
        return None

    mod.plugin.get_capability = fake_get_cap  # type: ignore[method-assign]

    # 管道步上下文无入站凭证：db-read run 锚反查降级（窗口记录 tenant 空），
    # 创建者 user_id 经 create_choice 透传 human 侧记录——记录归因回退
    # （_resolve_owner）支撑非 admin 创建者本人的决策面，归属不因锚降级丢失
    result = await mod.create_choice(
        title="t", options=["批准"], run_id="run-9", user_id="tenant-a"
    )
    assert result["status"] == "resolved"
    assert observed["during_wait"] == {"tenant": "", "user": "tenant-a"}, (
        "等待窗口内窗口记录在册（tenant 空由记录归因回退兜底），creator 随单透传"
    )
    owner = await mod._resolve_owner(hi, "req-win")
    assert owner == {"tenant": "tenant-a", "user": "tenant-a"}, (
        "锚反查降级时 human 记录归因回退必须给出创建者归属"
    )
    assert "req-win" not in mod._ownership, "窗口收敛后归属必须清除"


# ═══════════════════════════════════════════════════════════
# F8 租户边界显式化：run_id 反查走 db-read 契约（收窄内建+身份穿线）
# ═══════════════════════════════════════════════════════════


_EMPTY_ORIGIN = dict.fromkeys(
    ("pipeline_id", "thread_id", "agent_id", "agent_level", "agent_name"), ""
)


class TestTenantScopedRunAnchor:
    """run 锚反查数据面 = db-read pipeline_state tenant 档（C2 刀2）。

    收窄执法本体在内核 Rust 契约测试（read_faces/tests.rs）；本类钉插件侧
    装配契约：查询走 db-read、身份经 authorization 穿线、tenant_id 形参为
    fail-closed 闸（缺失不查询）、身份租户与谓词租户错位 = 过度限制不越界。
    """

    def test_same_run_id_cross_tenant_unreachable(self, tmp_path: Path) -> None:
        # 同值 run_id 只在租户 B 落行：租户 A 身份视角反查必须不可达（契约
        # 收窄包装按身份租户过滤）；本租户身份视角可达，反查回 B 行。
        # face 与 authorization 逐值穿线
        db = _make_state_db(tmp_path, [("pipe-b", "tenant-b", {"run_id": "run-x"})])
        mod = _load_server()
        fake = _install_db_read(mod, db)
        assert asyncio.run(mod._query_run_anchor("run-x", "tenant-a", _TOK_A)) is None
        assert asyncio.run(mod._lookup_run_tenant("run-x", "tenant-a", _TOK_A)) == ""
        assert asyncio.run(mod._lookup_run_origin("run-x", "tenant-a", _TOK_A)) == _EMPTY_ORIGIN
        # 本租户视角可达
        assert asyncio.run(mod._query_run_anchor("run-x", "tenant-b", _TOK_B)) == (
            "pipe-b",
            "tenant-b",
        )
        assert fake.calls[0]["face"] == "pipeline_state"
        assert fake.calls[0]["_authorization"] == _TOK_A

    def test_same_run_id_in_both_tenants_resolves_own_pipeline(self, tmp_path: Path) -> None:
        # 两租户各持同值 run_id（不同管道）：各自身份视角反查回各自管道行
        db = _make_state_db(
            tmp_path,
            [
                ("pipe-a", "tenant-a", {"run_id": "run-shared"}),
                ("pipe-b", "tenant-b", {"run_id": "run-shared"}),
            ],
        )
        mod = _load_server()
        _install_db_read(mod, db)
        assert asyncio.run(mod._query_run_anchor("run-shared", "tenant-a", _TOK_A)) == (
            "pipe-a",
            "tenant-a",
        )
        assert asyncio.run(mod._query_run_anchor("run-shared", "tenant-b", _TOK_B)) == (
            "pipe-b",
            "tenant-b",
        )

    def test_missing_tenant_scope_is_unattributable(self, tmp_path: Path) -> None:
        db = _make_runs_db(tmp_path, {"run-1": "tenant-a"})
        mod = _load_server()
        fake = _install_db_read(mod, db)
        # 租户缺失 = fail-closed 闸不过 → 不发起查询，直接不可归因
        assert asyncio.run(mod._query_run_anchor("run-1", "", _TOK_A)) is None
        assert asyncio.run(mod._lookup_run_tenant("run-1", "", _TOK_A)) == ""
        assert asyncio.run(mod._lookup_run_origin("run-1", "", _TOK_A)) == _EMPTY_ORIGIN
        assert fake.calls == []

    def test_anchor_with_empty_pipeline_id_returns_empty_origin(self, tmp_path: Path) -> None:
        # 锚行 pipeline_id 为空：来源五元组全空（前端按缺省省略来源段）
        db = _make_state_db(tmp_path, [("", "tenant-a", {"run_id": "run-e"})])
        mod = _load_server()
        _install_db_read(mod, db)
        assert asyncio.run(mod._query_run_anchor("run-e", "tenant-a", _TOK_A)) == ("", "tenant-a")
        assert asyncio.run(mod._lookup_run_origin("run-e", "tenant-a", _TOK_A)) == _EMPTY_ORIGIN

    def test_origin_second_query_failure_keeps_partial_origin(self, tmp_path: Path) -> None:
        # 锚命中后来源键查询契约失败：保留 pipeline_id，其余字段空（不阻塞主链路）
        db = _make_state_db(
            tmp_path,
            [("pipe-a", "tenant-a", {"run_id": "run-p", "session_id": "sess-9"})],
        )
        mod = _load_server()

        class _FailSecondQuery(FakeDbRead):
            """第二次调用（来源键 IN 查询）契约失败的替身。"""

            async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
                if "field_key IN" in str(params.get("sql", "")):
                    raise RuntimeError("bridge down on origin query")
                return await super().call(method, params, timeout)

        fake = _FailSecondQuery(db, {_TOK_A: "tenant-a"})
        mod.plugin._capabilities["db-read"] = fake
        origin = asyncio.run(mod._lookup_run_origin("run-p", "tenant-a", _TOK_A))
        assert origin == {**_EMPTY_ORIGIN, "pipeline_id": "pipe-a"}

    def test_identity_missing_fails_closed(self, tmp_path: Path) -> None:
        # 契约身份缺失（无凭证 → 内核 403 信封）→ 不可归因降级，绝不退化全租户
        db = _make_runs_db(tmp_path, {"run-1": "tenant-a"})
        mod = _load_server()
        _install_db_read(mod, db)
        fake = mod.plugin._capabilities["db-read"]
        fake._token_tenants = {}  # 无任何已认证身份
        assert asyncio.run(mod._query_run_anchor("run-1", "tenant-a", "")) is None

    def test_origin_display_returns_own_tenant_fields(self, tmp_path: Path) -> None:
        # 来源展示数据面经契约收窄通道正常返回；他租户行不串入拼装
        db = _make_state_db(
            tmp_path,
            [
                (
                    "pipe-a",
                    "tenant-a",
                    {
                        "run_id": "run-o",
                        "session_id": "sess-1",
                        "agent.id": "ag-1",
                        "agent_level": "L2",
                        "context.agent_name": "研究员",
                    },
                ),
                ("pipe-b", "tenant-b", {"run_id": "run-o"}),
            ],
        )
        mod = _load_server()
        _install_db_read(mod, db)
        origin = asyncio.run(mod._lookup_run_origin("run-o", "tenant-a", _TOK_A))
        assert origin == {
            "pipeline_id": "pipe-a",
            "thread_id": "sess-1",
            "agent_id": "ag-1",
            "agent_level": "L2",
            "agent_name": "研究员",
        }
        # 他租户视角只见本租户行：pipeline 直取，无来源字段（不跨租户补齐）
        assert asyncio.run(mod._lookup_run_origin("run-o", "tenant-b", _TOK_B)) == {
            **_EMPTY_ORIGIN,
            "pipeline_id": "pipe-b",
        }


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
