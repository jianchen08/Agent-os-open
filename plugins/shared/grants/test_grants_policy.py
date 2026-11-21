# @feature: FP-0.2.grants_ledger 授权账本 | @ci: python-coverage
"""grants 策略引擎（policy.py）测试——API 契约由本文件定义（TDD Red 先行）。

完成判据 3（执行方案 §1）：已分级键返回声明 verdict；未分类键返回 deny 且产生审计事件。
另覆盖：整工具键（单动作工具）、精确键优先、坏条目 fail-closed、mtime 缓存、
deny 审计经账本 grant_events 落库、policy.classify 服务 handler。

设计真值：ADR D2（未分类 fail-closed）+ 执行方案 §6（schema）。
红线锚：verdict 枚举含 per_call_approval——grant 不豁免 block/final_approval
的执法语义由 P1 落（本批只在枚举与注释保留该红线）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
for _p in (_SHARED_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ledger as grants_ledger  # noqa: E402 — 平铺插件模块
import policy as grants_policy  # noqa: E402

GrantsLedger = grants_ledger.GrantsLedger
PolicyEngine = grants_policy.PolicyEngine
load_policy_cached = grants_policy.load_policy_cached
resolve_policy_path = grants_policy.resolve_policy_path

_TABLE_V1 = {
    "version": 1,
    "defaults": {"uncategorized": "deny"},
    "tools": {
        "memory.retrieve": {"effect": "read", "verdict": "auto"},
        "memory.store": {"effect": "write", "verdict": "task_grant"},
        "memory.delete": {"effect": "write", "verdict": "per_call_approval"},
        "bash.execute": {"effect": "execute", "verdict": "task_grant"},
        "file_read": {"effect": "read", "verdict": "auto"},  # 单动作工具整工具键
        "project.transition": {"effect": "write", "verdict": "per_call_approval", "scope": "project_gate"},
    },
}


@pytest.fixture
def table_file(tmp_path: Path) -> Path:
    p = tmp_path / "tool_policy.yaml"
    p.write_text(yaml.safe_dump(_TABLE_V1, allow_unicode=True), encoding="utf-8")
    return p


class TestClassifiedKeys:
    def test_classified_key_returns_declared_verdict(self, table_file: Path) -> None:
        engine = PolicyEngine.from_yaml_file(str(table_file))
        got = engine.classify("memory", "retrieve")
        assert got["verdict"] == "auto"
        assert got["effect"] == "read"
        assert got["policy_key"] == "memory.retrieve"

    def test_per_call_approval_key(self, table_file: Path) -> None:
        engine = PolicyEngine.from_yaml_file(str(table_file))
        got = engine.classify("memory", "delete")
        assert got["verdict"] == "per_call_approval"

    def test_whole_tool_key_for_single_action_tool(self, table_file: Path) -> None:
        engine = PolicyEngine.from_yaml_file(str(table_file))
        got = engine.classify("file_read", "read")
        assert got["verdict"] == "auto"
        assert got["policy_key"] == "file_read"

    def test_exact_action_key_beats_whole_tool_key(self, table_file: Path) -> None:
        engine = PolicyEngine.from_yaml_file(str(table_file))
        got = engine.classify("memory", "store")
        assert got["verdict"] == "task_grant"
        assert got["policy_key"] == "memory.store"

    def test_scope_from_entry_carries_through(self, table_file: Path) -> None:
        engine = PolicyEngine.from_yaml_file(str(table_file))
        got = engine.classify("project", "transition")
        assert got["verdict"] == "per_call_approval"
        assert got["scope"] == "project_gate"


class TestUncategorizedFailClosed:
    def test_uncategorized_key_denies_and_audits(self, table_file: Path, tmp_path: Path) -> None:
        led = GrantsLedger(db_path=Path(tmp_path) / "grants.db")
        engine = PolicyEngine.from_yaml_file(str(table_file), ledger=led)
        got = engine.classify("totally_unknown_tool", "do_something")
        # 未分类 → deny（fail-closed，ADR D2；defaults.uncategorized 恒 deny 不受表数据摆布）
        assert got["verdict"] == "deny"
        assert got["policy_key"] is None
        # 审计事件落账本 grant_events
        deny_events = led.events(event_type="deny")
        assert len(deny_events) == 1
        ctx = deny_events[0]["context"]
        assert ctx["tool"] == "totally_unknown_tool"
        assert ctx["action"] == "do_something"
        assert ctx["reason"] == "unclassified"

    def test_uncategorized_without_ledger_denies_silently(self, table_file: Path) -> None:
        engine = PolicyEngine.from_yaml_file(str(table_file), ledger=None)
        got = engine.classify("totally_unknown_tool", "do_something")
        assert got["verdict"] == "deny"

    def test_empty_table_denies_everything(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text(yaml.safe_dump({"version": 1, "defaults": {"uncategorized": "deny"}, "tools": {}}), encoding="utf-8")
        engine = PolicyEngine.from_yaml_file(str(p))
        assert engine.classify("memory", "retrieve")["verdict"] == "deny"

    def test_non_mapping_table_falls_back_to_deny(self, tmp_path: Path) -> None:
        # yaml 顶层非映射（列表/标量）→ 视同空表，一切 deny
        p = tmp_path / "list.yaml"
        p.write_text(yaml.safe_dump(["memory.retrieve", "bash.execute"]), encoding="utf-8")
        engine = PolicyEngine.from_yaml_file(str(p))
        assert engine.classify("memory", "retrieve")["verdict"] == "deny"

    def test_defaults_uncategorized_cannot_flip_fail_closed(self, table_file: Path) -> None:
        # 表数据试图把未分类改成放行 → 仍 deny（fail-closed 由 D2 固化，非数据可配）
        p = table_file.parent / "evil.yaml"
        table = json.loads(json.dumps(_TABLE_V1))
        table["defaults"]["uncategorized"] = "auto"
        p.write_text(yaml.safe_dump(table), encoding="utf-8")
        engine = PolicyEngine.from_yaml_file(str(p))
        assert engine.classify("totally_unknown_tool", "do")["verdict"] == "deny"


class TestBadEntriesFailClosed:
    def test_unknown_verdict_value_denies(self, table_file: Path) -> None:
        p = table_file.parent / "bad_verdict.yaml"
        table = json.loads(json.dumps(_TABLE_V1))
        table["tools"]["memory.store"]["verdict"] = "yolo"
        p.write_text(yaml.safe_dump(table), encoding="utf-8")
        engine = PolicyEngine.from_yaml_file(str(p))
        got = engine.classify("memory", "store")
        assert got["verdict"] == "deny"
        assert got["reason"] == "invalid_entry"

    def test_unknown_effect_value_denies(self, table_file: Path) -> None:
        p = table_file.parent / "bad_effect.yaml"
        table = json.loads(json.dumps(_TABLE_V1))
        table["tools"]["memory.retrieve"]["effect"] = "teleport"
        p.write_text(yaml.safe_dump(table), encoding="utf-8")
        engine = PolicyEngine.from_yaml_file(str(p))
        assert engine.classify("memory", "retrieve")["verdict"] == "deny"

    def test_non_object_entry_denies(self, table_file: Path) -> None:
        p = table_file.parent / "bad_entry.yaml"
        table = json.loads(json.dumps(_TABLE_V1))
        table["tools"]["memory.retrieve"] = "auto"
        p.write_text(yaml.safe_dump(table), encoding="utf-8")
        engine = PolicyEngine.from_yaml_file(str(p))
        assert engine.classify("memory", "retrieve")["verdict"] == "deny"

    def test_invalid_entry_denies_audits_with_ledger(self, table_file: Path, tmp_path: Path) -> None:
        led = GrantsLedger(db_path=Path(tmp_path) / "grants.db")
        p = table_file.parent / "bad2.yaml"
        table = json.loads(json.dumps(_TABLE_V1))
        table["tools"]["memory.store"]["verdict"] = "yolo"
        p.write_text(yaml.safe_dump(table), encoding="utf-8")
        engine = PolicyEngine.from_yaml_file(str(p), ledger=led)
        engine.classify("memory", "store")
        ctx = led.events(event_type="deny")[0]["context"]
        assert ctx["reason"] == "invalid_entry"


class TestCache:
    def test_cached_load_reflects_file_change(self, table_file: Path) -> None:
        first = load_policy_cached(str(table_file))
        assert first.classify("memory", "retrieve")["verdict"] == "auto"
        # 改表：memory.retrieve → per_call_approval；mtime 显式推进（Windows mtime 粒度兜底）
        import os  # noqa: PLC0415

        table = json.loads(json.dumps(_TABLE_V1))
        table["tools"]["memory.retrieve"]["verdict"] = "per_call_approval"
        table_file.write_text(yaml.safe_dump(table), encoding="utf-8")
        st = table_file.stat()
        os.utime(table_file, ns=(st.st_atime_ns + 1_000_000, st.st_mtime_ns + 1_000_000))
        second = load_policy_cached(str(table_file))
        assert second.classify("memory", "retrieve")["verdict"] == "per_call_approval"

    def test_cached_load_same_file_same_instance(self, table_file: Path) -> None:
        assert load_policy_cached(str(table_file)) is load_policy_cached(str(table_file))


class TestResolvePolicyPath:
    def test_settings_override_wins(self, tmp_path: Path) -> None:
        got = resolve_policy_path({"tool_policy_file": str(tmp_path / "custom.yaml")})
        assert got == str(tmp_path / "custom.yaml")

    def test_default_points_at_repo_config(self) -> None:
        # 仓库布局：仓内 config 面 grants/tool_policy.yaml（装机 resources 同相对布局）
        repo_root = Path(_THIS_DIR).resolve().parents[2]
        assert Path(resolve_policy_path()) == repo_root / "config" / "plugins" / "grants" / "tool_policy.yaml"


class TestPolicyServiceHandler:
    """grants.policy.classify 服务 handler（server.py）契约。"""

    @pytest.fixture
    def server(self, table_file: Path, tmp_path: Path) -> object:
        import importlib.util  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location(
            "grants_p0_server_policy", str(Path(_THIS_DIR) / "server.py")
        )
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["grants_p0_server_policy"] = mod
        spec.loader.exec_module(mod)
        mod.reset_singletons()
        mod._LEDGER = grants_ledger.GrantsLedger(db_path=Path(tmp_path) / "grants.db")
        mod._POLICY = PolicyEngine.from_yaml_file(str(table_file), ledger=mod._LEDGER)
        return mod

    async def test_classify_service_contract(self, server: object) -> None:
        got = await server.svc_policy_classify(tool="memory", action="retrieve")
        assert got["verdict"] == "auto"
        got = await server.svc_policy_classify(tool="unknown_tool", action="run")
        assert got["verdict"] == "deny"

    async def test_ledger_service_roundtrip(self, server: object) -> None:
        issued = await server.svc_ledger_request(
            subject_kind="agent",
            subject_id="agent-1",
            tool="memory",
            action="store",
            granularity="task",
            source="approval_card",
        )
        assert issued["ok"] is True
        auth = await server.svc_ledger_authorize(
            subject_kind="agent", subject_id="agent-1", tool="memory", action="store"
        )
        assert auth["authorized"] is True
        listed = await server.svc_ledger_list(subject_id="agent-1")
        assert len(listed["grants"]) == 1
        consumed = await server.svc_ledger_consume(grant_id=issued["grant_id"])
        assert consumed["ok"] is True
        assert consumed["grant"]["uses"] == 1
        revoked = await server.svc_ledger_revoke(grant_id=issued["grant_id"])
        assert revoked["revoked"] is True
        consumed_after_revoke = await server.svc_ledger_consume(grant_id=issued["grant_id"])
        assert consumed_after_revoke["ok"] is False

    async def test_ledger_service_request_rejects_bad_enum(self, server: object) -> None:
        bad = await server.svc_ledger_request(
            subject_kind="alien",
            subject_id="agent-1",
            tool="memory",
            action="store",
            granularity="task",
            source="approval_card",
        )
        assert bad["ok"] is False
        assert "subject_kind" in bad["error"]

    def test_service_handlers_explicit_signature(self, server: object) -> None:
        """服务工具禁 **kwargs 透传（human 交互服务工具同款契约）。

        反向 capability 调用请求携带内核注入键（``_meta``/``config``），SDK 按
        handler 显式签名过滤；VAR_KEYWORD 全量透传会把注入键漏进账本 API
        （scratch 实测 2026-11-19：TypeError: unexpected keyword argument
        '_meta'）。六个服务面 handler（含 http.handle）一律显式签名——
        http.handle 消费注入键后的剩余键（透传安全），服务面则逐字对账。
        """
        import inspect  # noqa: PLC0415

        for name in (
            "svc_ledger_request",
            "svc_ledger_authorize",
            "svc_ledger_consume",
            "svc_ledger_revoke",
            "svc_ledger_list",
            "svc_policy_classify",
        ):
            handler = getattr(server, name)
            params = inspect.signature(handler).parameters.values()
            assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), (
                f"{name} 含 **kwargs（协议注入键会漏进服务实现）"
            )
