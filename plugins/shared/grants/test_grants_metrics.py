# @feature: FP-0.2.grants_ledger 授权账本 P3 运营面 | @ci: python-coverage
"""grants 指标端点（P3 运营面）测试——API 契约由本文件定义（TDD Red 先行）。

完成判据 1（任务书）：四指标各至少一断言（含数据源缺失降级断言）。

设计真值：执行方案 §9（指标口径）+ 任务书（只读聚合 grants/grant_events 两表 +
security_audit.jsonl 若可达；取不到的数据源如实降级 null + 说明，禁造数）。

指标口径（本文件即契约）：
- approval_card_popup_rate：弹卡次数 / 分级调用总数——弹卡事件在既有数据面无
  记录（账本只记授予结果，audit jsonl 只记 auto 放行/白名单/管道档授予），
  恒降级 null + 说明 + 可观测计数组件。
- grant_hit_rate：authorize 命中数 /（命中数 + 弹卡数）——率恒降级（同上）；
  省弹次数 = consume 事件数，完全可得，落 components.saved_popups。
- revoke_to_effect：撤销→下一次同类调用被拒墙钟差——被拒事件不经两源不可配对，
  恒降级 null + 说明；账本面生效证明 = 撤销后同 grant 零再消费
  （components.post_revoke_consumes，恒 0，>0 即异常）。
- uncategorized_blocked：classify 未分类 deny 事件数（ADR D2 fail-closed），
  完全可得。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
for _p in (_SHARED_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ledger as grants_ledger  # noqa: E402 — 平铺插件模块
import metrics as grants_metrics  # noqa: E402
import policy as grants_policy  # noqa: E402 — 平铺插件模块

if TYPE_CHECKING:
    from ledger import GrantsLedger  # 仅注解面类型名；运行时类对象经 grants_ledger 命名空间取

compute_metrics = grants_metrics.compute_metrics
read_security_audit = grants_metrics.read_security_audit
resolve_audit_path = grants_metrics.resolve_audit_path

METRIC_KEYS = ("approval_card_popup_rate", "grant_hit_rate", "revoke_to_effect", "uncategorized_blocked")


def _make_ledger(tmp_path: Path) -> GrantsLedger:
    return grants_ledger.GrantsLedger(db_path=Path(tmp_path) / "grants.db")


def _issue(led: GrantsLedger, **overrides: object) -> str:
    kwargs: dict = {
        "subject_kind": "agent",
        "subject_id": "agent-1",
        "tool": "memory",
        "action": "store",
        "granularity": "task",
        "source": "approval_card",
    }
    kwargs.update(overrides)
    return led.request(**kwargs)  # type: ignore[arg-type]


def _decode_body(resp: dict) -> str:
    import base64

    assert resp["success"] is True
    assert resp["data"]["body_encoding"] == "base64"
    return base64.b64decode(resp["data"]["body"]).decode("utf-8")


class TestResolveAuditPath:
    """审计文件落点解析（与 resolve_db_path 同构：settings 覆盖 > 用户数据根 > 共享根回落）。"""

    def test_settings_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
        monkeypatch.delenv("AGENTOS_DATA_DIR", raising=False)
        got = resolve_audit_path({"security_audit_file": str(tmp_path / "audit.jsonl")})
        assert got == str(tmp_path / "audit.jsonl")

    def test_user_data_root_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
        got = resolve_audit_path({"security_audit_file": str(tmp_path / "ignored.jsonl")})
        assert got == str(Path(tmp_path) / "data" / "security_audit.jsonl")

    def test_fallback_shared_root_dev_posture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
        monkeypatch.delenv("AGENTOS_DATA_DIR", raising=False)
        got = resolve_audit_path(None)
        assert Path(got) == Path(_SHARED_ROOT) / "data" / "security_audit.jsonl"

    def test_root_api_import_failure_skips_user_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _root_api 不可达（import 期失败）→ 跳过用户数据根，走 settings/共享根回落
        monkeypatch.setitem(sys.modules, "_root_api", None)
        override = tmp_path / "via-settings" / "audit.jsonl"
        assert resolve_audit_path({"security_audit_file": str(override)}) == str(override)


class TestReadSecurityAudit:
    """security_audit.jsonl 读取：可达/缺失/坏行跳过。"""

    def test_missing_file_is_unreachable_with_reason(self, tmp_path: Path) -> None:
        got = read_security_audit(str(tmp_path / "absent.jsonl"))
        assert got["reachable"] is False
        assert got["reason"]
        assert got["events"] == []

    def test_none_path_is_unreachable(self) -> None:
        assert read_security_audit(None)["reachable"] is False

    def test_readable_file_parses_lines_skips_malformed(self, tmp_path: Path) -> None:
        p = tmp_path / "audit.jsonl"
        p.write_text(
            json.dumps({"event": "grants_auto_allow", "tool": "memory"})
            + "\nnot-json-line\n"
            + json.dumps({"event": "whitelist_add", "signature": "sig-1"})
            + "\n",
            encoding="utf-8",
        )
        got = read_security_audit(str(p))
        assert got["reachable"] is True
        assert got["reason"] is None
        assert [e["event"] for e in got["events"]] == ["grants_auto_allow", "whitelist_add"]

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "audit.jsonl"
        p.write_text("\n" + json.dumps({"event": "grants_auto_allow"}) + "\n\n", encoding="utf-8")
        got = read_security_audit(str(p))
        assert got["reachable"] is True
        assert [e["event"] for e in got["events"]] == ["grants_auto_allow"]


def _audit(events: list[dict]) -> dict:
    return {"reachable": True, "reason": None, "events": events}


class TestComputeMetricsUncategorizedBlocked:
    """未分类拦截数：完全可得（ADR D2 fail-closed 计数）。"""

    def test_counts_unclassified_deny_events(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        engine = grants_policy.PolicyEngine({"tools": {}}, ledger=led)
        engine.classify("unknown_a", "run")
        engine.classify("unknown_b", "run")
        led.append_event(event_type="deny", context={"tool": "x", "action": "y", "reason": "invalid_entry"})
        result = compute_metrics(led.events(), _audit([]))
        metric = result["metrics"]["uncategorized_blocked"]
        assert metric["value"] == 2
        assert metric["degraded"] is False
        assert metric["components"]["unclassified"] == 2
        assert metric["components"]["invalid_entry"] == 1

    def test_empty_ledger_zero_counts(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        result = compute_metrics(led.events(), {"reachable": False, "reason": "missing", "events": []})
        assert result["metrics"]["uncategorized_blocked"]["value"] == 0


class TestComputeMetricsGrantHitRate:
    """grant 命中率（省弹次数）：率降级 + 省弹次数完全可得。"""

    def test_saved_popups_counted_and_rate_degraded(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        for _ in range(3):
            grant_id = _issue(led)
            led.consume(grant_id=grant_id)
        _issue(led, source="oauth_broker")
        result = compute_metrics(led.events(), _audit([]))
        metric = result["metrics"]["grant_hit_rate"]
        assert metric["value"] is None
        assert metric["degraded"] is True
        assert metric["reason"]
        assert metric["components"]["saved_popups"] == 3
        assert metric["components"]["approval_card_issues"] == 3  # 3 次 issue（source=approval_card）

    def test_empty_ledger_zero_saved_popups(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        metric = compute_metrics(led.events(), _audit([]))["metrics"]["grant_hit_rate"]
        assert metric["components"]["saved_popups"] == 0


class TestComputeMetricsPopupRate:
    """审批卡弹出率：弹卡事件无记录面 → 恒降级 + 可观测计数组件。"""

    def test_degraded_with_audit_components(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led)
        _issue(led)
        audit = _audit(
            [
                {"event": "grants_auto_allow"},
                {"event": "grants_auto_allow"},
                {"event": "mode_switch", "action": "grant"},
                {"event": "mode_switch", "action": "revoke"},
                {"event": "whitelist_add"},
            ]
        )
        metric = compute_metrics(led.events(), audit)["metrics"]["approval_card_popup_rate"]
        assert metric["value"] is None
        assert metric["degraded"] is True
        assert metric["reason"]
        assert metric["components"]["approval_card_grant_issues"] == 2
        assert metric["components"]["auto_allow_events"] == 2
        assert metric["components"]["pipeline_mode_grants"] == 1  # 只数 action=grant
        assert metric["components"]["whitelist_adds"] == 1

    def test_audit_unreachable_components_null(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led)
        metric = compute_metrics(led.events(), {"reachable": False, "reason": "missing", "events": []})[
            "metrics"
        ]["approval_card_popup_rate"]
        assert metric["components"]["approval_card_grant_issues"] == 1
        assert metric["components"]["auto_allow_events"] is None
        assert metric["components"]["pipeline_mode_grants"] is None
        assert metric["components"]["whitelist_adds"] is None


class TestComputeMetricsRevokeToEffect:
    """撤销→生效耗时：被拒事件不可配对 → 恒降级；账本面生效证明 = 撤销后零再消费。"""

    def test_degraded_with_zero_stale_consumes(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        grant_id = _issue(led)
        led.consume(grant_id=grant_id)
        led.revoke(grant_id=grant_id)
        result = compute_metrics(led.events(), _audit([]))
        metric = result["metrics"]["revoke_to_effect"]
        assert metric["value"] is None
        assert metric["degraded"] is True
        assert metric["reason"]
        assert metric["components"]["revokes"] == 1
        assert metric["components"]["post_revoke_consumes"] == 0

    def test_post_revoke_consume_detected_as_stale(self, tmp_path: Path) -> None:
        """撤销后同 grant 再消费 = 生效异常，计数器必须能抓到（防拟合区分度输入）。"""
        led = _make_ledger(tmp_path)
        grant_id = _issue(led)
        led.revoke(grant_id=grant_id)
        # 直接追加一条晚于 revoke 的 consume 事件（构造账本面异常形态：真实 API 下
        # consume 对 revoked grant 会 raise，此形态只可能来自并发/时钟边界）
        led.append_event(event_type="consume", grant_id=grant_id, context={"tool": "memory"})
        metric = compute_metrics(led.events(), _audit([]))["metrics"]["revoke_to_effect"]
        assert metric["components"]["post_revoke_consumes"] == 1


class TestComputeMetricsShape:
    """四指标闭集 + 来源面报告。"""

    def test_four_metric_keys_and_sources_block(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        result = compute_metrics(led.events(), {"reachable": False, "reason": "missing", "events": []})
        assert set(result["metrics"].keys()) == set(METRIC_KEYS)
        assert result["sources"]["security_audit_jsonl"]["reachable"] is False

    def test_audit_reachability_reported(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        result = compute_metrics(led.events(), _audit([{"event": "grants_auto_allow"}]))
        assert result["sources"]["security_audit_jsonl"]["reachable"] is True


class TestMetricsEndpoint:
    """GET /ext/grants/metrics（auth=user 声明面；本测试直驱 http_handle）。"""

    @pytest.fixture
    def server(self, tmp_path: Path) -> object:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "grants_p3_server_metrics", str(Path(_THIS_DIR) / "server.py")
        )
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["grants_p3_server_metrics"] = mod
        spec.loader.exec_module(mod)
        mod.reset_singletons()
        mod._LEDGER = _make_ledger(tmp_path)
        audit_file = tmp_path / "security_audit.jsonl"
        audit_file.write_text(json.dumps({"event": "grants_auto_allow"}) + "\n", encoding="utf-8")
        mod.plugin._injected_config = {"settings": {"security_audit_file": str(audit_file)}}
        return mod

    @pytest.mark.asyncio
    async def test_metrics_endpoint_returns_four_metrics(self, server: object) -> None:
        grant_id = _issue(server._LEDGER)  # type: ignore[attr-defined]
        server._LEDGER.consume(grant_id=grant_id)  # type: ignore[attr-defined]
        resp = await server.http_handle(path="/ext/grants/metrics", method="GET")
        assert resp["data"]["status"] == 200
        body = json.loads(_decode_body(resp))
        assert set(body["metrics"].keys()) == set(METRIC_KEYS)
        assert body["metrics"]["grant_hit_rate"]["components"]["saved_popups"] == 1
        assert body["sources"]["security_audit_jsonl"]["reachable"] is True
        assert body["metrics"]["approval_card_popup_rate"]["components"]["auto_allow_events"] == 1

    @pytest.mark.asyncio
    async def test_metrics_endpoint_audit_missing_degrades(self, server: object) -> None:
        server.plugin._injected_config = {"settings": {"security_audit_file": "Z:/definitely/absent.jsonl"}}
        resp = await server.http_handle(path="/ext/grants/metrics", method="GET")
        body = json.loads(_decode_body(resp))
        assert body["sources"]["security_audit_jsonl"]["reachable"] is False
        assert body["sources"]["security_audit_jsonl"]["reason"]
        assert body["metrics"]["approval_card_popup_rate"]["components"]["auto_allow_events"] is None
