# @feature: FP-0.2.grants_ledger P2 mandate 契约 | @ci: python-coverage
"""grants.ledger.authorize mandate 契约位（P2 跨端委派契约 + 测试桩，执行方案 §4）。

契约（ADR 2026-11-19 D1 + docs/working/跨端执行面_工作空间属地Agent网络方案_20261118.md
「上线顺序约束」：每次跨端委派携带 mandate（scope/时限/task_id），**无 grant 不派**）：

1. **四要素校验**（服务面承载，可本地判定面；验签留给跨端执行面 delegation——尚未
   立项，本批只落契约 + 测试桩，不实现远端）：
   - scope：必填非空对象，且逐键 ⊆ 命中 grant 的 scope（委派范围不得超出授予范围）；
   - 时限：必填、ISO 8601 可解析（不可解析 fail-closed）、未过期；
   - task_id：必填非空；调用方声明执行任务 id 时必须逐字相符（绑定校验）；
   - 可撤销：恒须 ``revocable is True``（不可撤销的 mandate 一律拒）。
2. **无 grant / 无 mandate 不放行**：mandate 校验模式下账本无命中 grant → 拒
   （``no_grant``）；声明 ``require_mandate=True`` 而未携带 mandate → 拒
   （``mandate_absent``）。撤销/过期的 grant 同样不放行（生命周期兑现）。
3. **向后兼容**：``mandate``/``require_mandate`` 皆缺省 = 既有路径，raw 账本方法
   与服务面返回形态零变化（P0/P1 测试零回归）。

驱动面 = 真实服务实现 ``server.svc_ledger_authorize``（生产装配形态，唯一模块名
装载防同名串台）；账本 = 真实 GrantsLedger（tmp 隔离库）。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
for _p in (_SHARED_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ledger import GrantsLedger, validate_mandate  # noqa: E402 — 平铺插件模块

pytestmark = pytest.mark.unit

_SERVER_URL = "https://mcp.example.com/mcp"


# ── 真实服务实现装载（server.py 生产装配形态；每用例独立账本）─────────────────


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "grants_p2_server_mandate", str(Path(_THIS_DIR) / "server.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["grants_p2_server_mandate"] = mod
    spec.loader.exec_module(mod)
    mod.reset_singletons()
    mod._LEDGER = GrantsLedger(db_path=str(tmp_path / "mandate" / "grants.db"))
    return mod


def _issue_domain_grant(
    led: GrantsLedger, *, scope: dict[str, Any] | None = None, expires_at: str | None = None
) -> str:
    return led.request(
        subject_kind="user",
        subject_id="*",
        tool="mcp",
        action=_SERVER_URL,
        granularity="domain",
        source="oauth_broker",
        scope=scope if scope is not None else {"domain": _SERVER_URL},
        expires_at=expires_at,
    )


async def _authorize_mandate(
    svc: Any, mandate: Any, *, task_id: str | None = "task-1", **kwargs: Any
) -> dict[str, Any]:
    return await svc.svc_ledger_authorize(
        subject_kind="user",
        subject_id="*",
        tool="mcp",
        action=_SERVER_URL,
        mandate=mandate,
        task_id=task_id,
        **kwargs,
    )


def _mandate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scope": {"domain": _SERVER_URL},
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "task_id": "task-1",
        "revocable": True,
    }
    base.update(overrides)
    # ...（Ellipsis）占位 = 剔除该键（「缺失」要素的构造法）
    return {k: v for k, v in base.items() if v is not ...}


def _reason(result: dict[str, Any]) -> Any:
    mandate = result.get("mandate")
    assert isinstance(mandate, dict), f"mandate 报告缺失: {result}"
    assert mandate["valid"] is False
    return mandate["reason"]


# ────────────────────────────────────────────────────────────
# 1. 正面：四要素齐备 + grant 命中 → 放行
# ────────────────────────────────────────────────────────────


async def test_valid_mandate_with_grant_authorizes(svc: Any) -> None:
    grant_id = _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, _mandate())
    assert result["authorized"] is True
    assert result["mandate"]["valid"] is True
    assert result["mandate"]["reason"] is None
    assert result["grant"]["grant_id"] == grant_id


async def test_scope_exact_and_multi_key_subset_pass(svc: Any) -> None:
    _issue_domain_grant(svc._LEDGER, scope={"domain": _SERVER_URL, "resource": _SERVER_URL})
    exact = await _authorize_mandate(svc, _mandate(scope={"domain": _SERVER_URL, "resource": _SERVER_URL}))
    assert exact["authorized"] is True, "mandate scope 逐键等于 grant scope → 放行"
    subset = await _authorize_mandate(svc, _mandate(scope={"domain": _SERVER_URL}))
    assert subset["authorized"] is True, "mandate scope 是 grant scope 子集 → 放行"


# ────────────────────────────────────────────────────────────
# 2. 桩断言：无 grant / 无 mandate 不放行
# ────────────────────────────────────────────────────────────


async def test_no_grant_blocks_mandate(svc: Any) -> None:
    result = await _authorize_mandate(svc, _mandate())  # 空账本
    assert result["authorized"] is False
    assert result["grant"] is None
    assert _reason(result) == "no_grant", "无 grant 不放行（跨端上线顺序约束原文）"


async def test_missing_mandate_blocks_when_required(svc: Any) -> None:
    _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, None, require_mandate=True)
    assert result["authorized"] is False
    assert result["grant"] is None
    assert _reason(result) == "mandate_absent", "无 mandate 不放行（require_mandate 声明强制）"


# ────────────────────────────────────────────────────────────
# 3. 四要素逐项校验
# ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scope", "expected_reason"),
    [
        ("https://mcp.example.com/mcp", "scope_invalid"),  # 非对象
        ({}, "scope_invalid"),  # 空对象
        (..., "scope_invalid"),  # 键缺失（... 占位 → _mandate 剔键）
        ({"domain": "https://other.example.com/mcp"}, "scope_exceeds_grant"),  # 超出授予域
        ({"domain": _SERVER_URL, "extra": "x"}, "scope_exceeds_grant"),  # grant 无此键
    ],
)
async def test_scope_element_validation(svc: Any, scope: Any, expected_reason: str) -> None:
    _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, _mandate(scope=scope))
    assert result["authorized"] is False
    assert _reason(result) == expected_reason


@pytest.mark.parametrize(
    ("expires_at", "expected_reason"),
    [
        (..., "expires_missing"),
        ("", "expires_missing"),
        ("not-a-timestamp", "expires_unparseable"),  # fail-closed：不可解析视同无效
        ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), "mandate_expired"),
    ],
)
async def test_expiry_element_validation(svc: Any, expires_at: Any, expected_reason: str) -> None:
    _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, _mandate(expires_at=expires_at))
    assert result["authorized"] is False
    assert _reason(result) == expected_reason


@pytest.mark.parametrize(
    ("mandate_task", "call_task", "expected_reason"),
    [
        (..., "task-1", "task_id_missing"),
        ("", "task-1", "task_id_missing"),
        ("task-1", "task-2", "task_id_mismatch"),  # 绑定校验：与执行任务不符
    ],
)
async def test_task_id_element_validation(
    svc: Any, mandate_task: Any, call_task: str, expected_reason: str
) -> None:
    _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, _mandate(task_id=mandate_task), task_id=call_task)
    assert result["authorized"] is False
    assert _reason(result) == expected_reason


async def test_task_id_binding_absent_call_context_passes_on_presence(svc: Any) -> None:
    """调用方未声明执行任务 id（task_id=None）→ 只做 task_id 存在性校验（绑定深度
    留给跨端执行面）。"""
    _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, _mandate(), task_id=None)
    assert result["authorized"] is True


@pytest.mark.parametrize("revocable", [False, ...])
async def test_revocable_element_required(svc: Any, revocable: Any) -> None:
    _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, _mandate(revocable=revocable))
    assert result["authorized"] is False
    assert _reason(result) == "not_revocable", "不可撤销的 mandate 一律拒"


@pytest.mark.parametrize("malformed", ["mandate-as-string", ["mandate-as-list"], 42])
async def test_malformed_mandate_rejected(svc: Any, malformed: Any) -> None:
    _issue_domain_grant(svc._LEDGER)
    result = await _authorize_mandate(svc, malformed)
    assert result["authorized"] is False
    assert _reason(result) == "mandate_malformed"


# ────────────────────────────────────────────────────────────
# 4. grant 生命周期在 mandate 模式下同样兑现（可撤销性闭环）
# ────────────────────────────────────────────────────────────


async def test_revoked_grant_blocks_mandate(svc: Any) -> None:
    grant_id = _issue_domain_grant(svc._LEDGER)
    assert svc._LEDGER.revoke(grant_id=grant_id) is True
    result = await _authorize_mandate(svc, _mandate())
    assert result["authorized"] is False
    assert _reason(result) == "no_grant", "撤销后在 mandate 模式同样不放行"


async def test_expired_grant_blocks_mandate(svc: Any) -> None:
    _issue_domain_grant(svc._LEDGER, expires_at=(datetime.now(UTC) - timedelta(seconds=5)).isoformat())
    result = await _authorize_mandate(svc, _mandate())
    assert result["authorized"] is False
    assert _reason(result) == "no_grant", "过期 grant 在 mandate 模式同样不放行"


# ────────────────────────────────────────────────────────────
# 5. 向后兼容：raw 账本方法与服务面缺省路径形态零变化
# ────────────────────────────────────────────────────────────


def test_raw_ledger_authorize_shape_unchanged(tmp_path: Path) -> None:
    led = GrantsLedger(db_path=str(tmp_path / "raw" / "grants.db"))
    grant_id = _issue_domain_grant(led)
    hit = led.authorize(subject_kind="user", subject_id="*", tool="mcp", action=_SERVER_URL)
    assert hit is not None, "raw 命中形态 = grant dict（P0 原样）"
    assert hit["grant_id"] == grant_id
    assert led.authorize(subject_kind="user", subject_id="*", tool="bash", action="run") is None


async def test_service_default_path_shape_unchanged(svc: Any) -> None:
    grant_id = _issue_domain_grant(svc._LEDGER)
    hit = await svc.svc_ledger_authorize(subject_kind="user", subject_id="*", tool="mcp", action=_SERVER_URL)
    assert set(hit.keys()) == {"authorized", "grant"}, "服务面缺省路径形态零变化"
    assert hit["authorized"] is True
    assert hit["grant"]["grant_id"] == grant_id
    miss = await svc.svc_ledger_authorize(subject_kind="user", subject_id="*", tool="bash", action="run")
    assert set(miss.keys()) == {"authorized", "grant"}
    assert miss["authorized"] is False
    assert miss["grant"] is None


# ────────────────────────────────────────────────────────────
# 6. validate_mandate 纯函数面（模块级可复用，delegation 执行面承接位）
# ────────────────────────────────────────────────────────────


def test_validate_mandate_pure_function_direct() -> None:
    grant = {"scope": {"domain": _SERVER_URL}}
    check = validate_mandate(_mandate(), grant=grant, task_id="task-1")
    assert check["valid"] is True
    bad = validate_mandate(_mandate(scope={"domain": "https://other.example.com"}), grant=grant)
    assert bad["valid"] is False
    assert bad["reason"] == "scope_exceeds_grant"
    absent_grant = validate_mandate(_mandate(), grant=None)
    assert absent_grant["valid"] is False
    assert absent_grant["reason"] == "scope_exceeds_grant", "无 grant 时 scope 子集校验按空 grant 收紧拒"
