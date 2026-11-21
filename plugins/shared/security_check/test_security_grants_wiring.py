# @feature: FP-0.2.grants_ledger P1 执法接线 | @ci: python-coverage
"""security_check × grants 执法接线行为锁（P1 七断言，执行方案 §2 验收 ①–⑦）。

契约（ADR 2026-11-19-grants-ledger-and-tool-classification D1–D3 + ADR 2026-11-12 语义基准）：
1. ① auto 类调用无审批卡、无账本写（账本零依赖照常）；
2. ② file_write 类第二次同类写 grant 命中不弹卡且账本 uses+1；
3. ③ dangerous_commands 规则覆盖的命令仍逐次弹卡（规则面优先，grant 永不豁免规则裁决）；
4. ④ revoke 后同类调用重弹卡；
5. ⑤ grants 服务不可用/异常 → inert 放行 + grants_degraded 降级横幅每管道一次
   （既有审批语义零变化——等价于 grants 面不存在；用户裁定 2026-11-19）；
6. ⑥ final_approval: true 规则不受任何 grant 影响（仍逐次人审）；
7. ⑦ 旁路档下 device_control/financial 类 verdict 仍走逐次人审（D3：免审批摩擦不免高风险类）。

分级真源 = 仓内 `config/plugins/grants/tool_policy.yaml`（真实 PolicyEngine 消费）；
账本 = 真实 GrantsLedger（tmp_path 隔离库）。审批卡五选项/端点/终审/血缘链零变化（R3）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
_GRANTS_DIR = str(Path(_SHARED_DIR) / "grants")
_REPO_ROOT = str(Path(_SHARED_DIR).resolve().parents[1])
for _p in (_SHARED_DIR, _THIS_DIR, _GRANTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_grants_wiring", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from ledger import GrantsLedger  # noqa: E402 — grants 平铺模块
from policy import PolicyEngine  # noqa: E402

# 仓内真实分级表（关键路径真实依赖：分级真值不另造测试表）
_TOOL_POLICY_YAML = Path(_REPO_ROOT) / "config" / "plugins" / "grants" / "tool_policy.yaml"

pytestmark = pytest.mark.unit


# ── 假 capability：形态与 grants server.py 服务返回结构一致 ──────────────────


class FakePolicyCap:
    """grants.policy 假 capability：真实 PolicyEngine 消费仓内策略表；可注入异常/覆盖。"""

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        fail: bool = False,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.engine = engine
        self.fail = fail
        self.overrides = dict(overrides or {})
        self.calls: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        assert method == "classify", f"unexpected policy method: {method}"
        self.calls.append(dict(params))
        if self.fail:
            raise RuntimeError("grants policy service boom")
        key = f"{params.get('tool')}.{params.get('action')}"
        if key in self.overrides:
            return dict(self.overrides[key])
        if params.get("action"):
            return self.engine.classify(str(params["tool"]), str(params["action"]))
        return self.engine.classify(str(params["tool"]), "")

    def classify_calls(self) -> int:
        return len(self.calls)


class FakeLedgerCap:
    """grants.ledger 假 capability：真实 GrantsLedger 承载；可注入异常/拒绝形态。"""

    def __init__(
        self,
        ledger: GrantsLedger,
        *,
        fail: bool = False,
        reject_request: bool = False,
        consume_fail: bool = False,
    ) -> None:
        self.ledger = ledger
        self.fail = fail
        self.reject_request = reject_request
        self.consume_fail = consume_fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        self.calls.append((method, dict(params)))
        if self.fail:
            raise RuntimeError("grants ledger service boom")
        if method == "authorize":
            grant = self.ledger.authorize(
                subject_kind=str(params["subject_kind"]),
                subject_id=str(params["subject_id"]),
                tool=str(params["tool"]),
                action=str(params["action"]),
            )
            return {"authorized": grant is not None, "grant": grant}
        if method == "request":
            if self.reject_request:
                return {"ok": False, "error": "rejected by policy"}
            grant_id = self.ledger.request(**params)
            return {"ok": True, "grant_id": grant_id}
        if method == "consume":
            if self.consume_fail:
                raise RuntimeError("consume channel boom")
            try:
                grant = self.ledger.consume(grant_id=str(params["grant_id"]), context=params.get("context"))
            except Exception as exc:  # noqa: BLE001 — 消费失败形态与 server 服务一致
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "grant": grant}
        if method == "revoke":
            return {"ok": True, "revoked": self.ledger.revoke(grant_id=str(params["grant_id"]))}
        raise AssertionError(f"unexpected ledger method: {method}")

    def ledger_calls(self, method: str) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if c[0] == method]


class FakeHiCap:
    """human-interaction 假 capability：按序回放选中项并按卡上声明回语义。"""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.cards: list[dict[str, Any]] = []
        self._last_options: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        if method == "create_choice":
            self.cards.append(params)
            self._last_options = list(params.get("options") or [])
            return {"request_id": f"req-{len(self.cards)}"}
        if method == "wait_for_choice":
            assert self.answers, "审批次数超出预期（answers 耗尽）"
            answer = self.answers.pop(0)
            semantics = ""
            for opt in self._last_options:
                if opt.get("id") == answer:
                    semantics = str(opt.get("semantics") or "")
                    break
            return {"selected_option": answer, "selected_semantics": semantics, "response_type": "answered"}
        raise AssertionError(f"unexpected hi cap.call: {method}")


class _EmitRecorder:
    """记录 frontend.emit 调用的装配缝（外部通道边界，显式注入）——降级横幅断言用。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], str]] = []

    async def __call__(self, event: str, payload: dict[str, Any], thread_id: str) -> None:
        self.events.append((event, payload, thread_id))

    def of(self, event: str) -> list[tuple[str, dict[str, Any], str]]:
        return [e for e in self.events if e[0] == event]


# ── 装配 ────────────────────────────────────────────────────────────────────


def _engine() -> PolicyEngine:
    return PolicyEngine.from_yaml_file(str(_TOOL_POLICY_YAML))


def _wire(
    answers: list[str],
    *,
    policy_fail: bool = False,
    ledger_fail: bool = False,
    policy_overrides: dict[str, dict[str, Any]] | None = None,
    with_grants: bool = True,
    reject_request: bool = False,
    consume_fail: bool = False,
) -> tuple[FakeHiCap, FakePolicyCap | None, FakeLedgerCap | None, GrantsLedger]:
    assert _STATE_DIR is not None, "夹具未就绪（_STATE_DIR 未注入）"
    hi = FakeHiCap(answers)
    _mod.set_human_interaction_cap(hi)
    ledger = GrantsLedger(db_path=str(_STATE_DIR / "grants.db"))
    if with_grants:
        policy_cap = FakePolicyCap(_engine(), fail=policy_fail, overrides=policy_overrides)
        ledger_cap = FakeLedgerCap(ledger, fail=ledger_fail, reject_request=reject_request, consume_fail=consume_fail)
        _mod.set_grants_caps(policy_cap, ledger_cap)
        return hi, policy_cap, ledger_cap, ledger
    _mod.set_grants_caps(None, None)
    return hi, None, None, ledger


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """每测试独立存储与表（不碰用户数据根/共享根/仓内配置）。"""
    global _STATE_DIR  # noqa: PLW0603
    _STATE_DIR = tmp_path
    scope_mod = sys.modules["approval_scope"]
    monkeypatch.setattr(_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "permission_modes.json"))
    monkeypatch.setattr(_mod, "_PERMISSION_MODES", {})
    scope_mod.PIPELINE_PERMISSION_MODES.clear()
    scope_mod.PIPELINE_PARENTS.clear()
    scope_mod.PERMANENT_WHITELIST.clear()
    _mod.set_human_interaction_cap(None)
    _mod.set_grants_caps(None, None)
    _mod.set_grants_ref_resolver(None)
    _mod.set_frontend_emit(None)
    yield tmp_path
    _mod.set_human_interaction_cap(None)
    _mod.set_grants_caps(None, None)
    _mod.set_grants_ref_resolver(None)
    _mod.set_frontend_emit(None)
    scope_mod.PIPELINE_PERMISSION_MODES.clear()
    scope_mod.PIPELINE_PARENTS.clear()
    scope_mod.PERMANENT_WHITELIST.clear()
    _STATE_DIR = None


_STATE_DIR: Path | None = None


def _ctx(
    *,
    pipeline_id: str = "p1",
    session_id: str = "s1",
    calls: list[dict[str, Any]] | None = None,
    tool: str = "bash",
    command: str = "pip install requests",
    state_extra: dict[str, Any] | None = None,
) -> Any:
    if calls is None:
        calls = [{"name": tool, "args": {"command": command}}]
    state: dict[str, Any] = {
        "core_type": "tool_execute",
        "raw_tool_calls": calls,
        "execution_contexts": [{"tool_name": c.get("name", tool), "provider": "host"} for c in calls],
        "pipeline_id": pipeline_id,
        "session_id": session_id,
        "user_id": "u1",
    }
    if state_extra:
        state.update(state_extra)
    return _mod.PluginContext(state=state, _services={})


def _rules() -> list[dict[str, Any]]:
    """最小非降级规则集：dangerous_commands（needs_approval）。"""
    return [
        {
            "name": "dangerous_commands",
            "tools": ["*"],
            "params": ["command", "cmd"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "pip install"}],
        }
    ]


def _blocked(result: Any) -> bool:
    return bool(result.state_updates.get("pre_decided_results"))


# ②④ 共用装配：file_write 注入危险声明（write:D:/ 前缀），规则面不覆盖 D:/ 写
_FILE_WRITE_DANGEROUS_CONFIG = {
    "builtin_tools_config": {
        "tools": [{"name": "file_write", "dangerous_operations": ["write:D:/"]}]
    }
}


def _file_write_call(path: str) -> list[dict[str, Any]]:
    return [{"name": "file_write", "args": {"path": path, "action": "write", "content": "x"}}]


# ── ① auto 类调用：无审批卡、无账本写 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_class_no_card_no_ledger_write() -> None:
    """①a 常规读调用（memory.retrieve，非危险不进授权轨）零卡零账本调用。"""
    hi, policy_cap, ledger_cap, _ledger = _wire([])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    result = await plugin.execute(_ctx(tool="memory", command="", calls=[
        {"name": "memory", "args": {"action": "retrieve", "query": "x"}},
    ]))

    assert not _blocked(result)
    assert hi.cards == [], "auto 类不得弹卡"
    assert ledger_cap is not None
    assert ledger_cap.calls == [], "auto 类零账本调用（authorize/request/consume 均不得发生）"


@pytest.mark.asyncio
async def test_auto_class_dangerous_gate_entry_still_no_card_no_ledger_write() -> None:
    """①b 危险闸进入但分级 auto（file_read，注入 read:/data/ 危险声明）：放行+审计，零卡零账本。"""
    hi, policy_cap, ledger_cap, _ledger = _wire([])
    config = {
        "enabled": True,
        "rules": _rules(),
        "builtin_tools_config": {
            "tools": [{"name": "file_read", "dangerous_operations": ["read:/data/"]}]
        },
    }
    plugin = _mod.SecurityCheckPlugin(config=config)

    result = await plugin.execute(_ctx(calls=[
        {"name": "file_read", "args": {"path": "/data/notes.txt"}},
    ]))

    assert not _blocked(result), "auto 类调用放行"
    assert hi.cards == []
    assert policy_cap is not None, "policy cap 须在位"
    assert policy_cap.classify_calls() == 1, "危险闸进入后应有一次 classify"
    assert ledger_cap is not None, "ledger cap 须在位"
    assert ledger_cap.calls == [], "auto 类零账本写"
    assert _STATE_DIR is not None
    audit = (_STATE_DIR / "security_audit.jsonl").read_text(encoding="utf-8")
    assert "grants_auto_allow" in audit, "auto 放行须落审计事件（异步审计面）"
    assert "file_read" in audit


# ── ② file_write 第二次同类写 grant 命中不弹卡且 uses+1 ─────────────────────


@pytest.mark.asyncio
async def test_file_write_second_same_class_write_grant_hit_no_card_and_uses_increment() -> None:
    """② 首写 miss 弹卡 → approved_remember 双写 task grant → 同类第二写命中放行 + uses+1。"""
    hi, _policy, ledger_cap, ledger = _wire(["approved_remember"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG})

    first = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    assert _blocked(first) is False, "首写经审批卡批准放行"
    assert len(hi.cards) == 1, "首写 miss 账本 → 既有审批卡"
    # 双写：账本出现 pipeline 主体 task 粒度 grant（tool=file_write, action=write）
    rows = ledger.list(subject_kind="pipeline", subject_id="p1")
    assert len(rows) == 1
    grant = rows[0]
    assert grant["tool"] == "file_write"
    assert grant["action"] == "write"
    assert grant["granularity"] == "task"
    assert grant["source"] == "approval_card"

    second = await plugin.execute(_ctx(calls=_file_write_call("D:/out/b.txt")))
    assert not _blocked(second), "同类第二写 grant 命中放行不弹卡"
    assert len(hi.cards) == 1, "第二写不再建卡"
    after = ledger.list(subject_kind="pipeline", subject_id="p1")[0]
    assert after["uses"] == 1, "grant 命中须消费记账 uses+1"
    assert after["status"] == "active"
    assert ledger_cap is not None
    assert len(ledger_cap.ledger_calls("consume")) == 1


# ── ③ dangerous_commands 覆盖命令仍逐次弹卡 ────────────────────────────────


@pytest.mark.asyncio
async def test_dangerous_commands_rule_still_cards_every_time() -> None:
    """③ 规则面优先：dangerous_commands 命中的命令逐次弹卡——无 grant 弹、有 grant 仍弹且不消费。"""
    hi, _policy, _ledger_cap, ledger = _wire(["approved_once", "approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})

    first = await plugin.execute(_ctx(command="pip install requests"))
    assert _blocked(first) is False, "第一次经卡批准放行"
    assert len(hi.cards) == 1

    # 预置一条 bash.execute 的 task grant（另一管道授予的账本行）——规则覆盖的调用不受其豁免
    ledger.request(
        subject_kind="pipeline",
        subject_id="p-other",
        tool="bash",
        action="execute",
        granularity="task",
        source="approval_card",
    )

    second = await plugin.execute(_ctx(command="pip install other", pipeline_id="p1"))
    assert _blocked(second) is False
    assert len(hi.cards) == 2, "dangerous_commands 覆盖命令仍逐次弹卡（规则面优先，grant 不豁免）"
    row = next(g for g in ledger.list(subject_kind="pipeline", subject_id="p-other") if g["tool"] == "bash")
    assert row["uses"] == 0, "规则覆盖的调用不得消费 grant"


# ── ④ revoke 后同类调用重弹卡 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_makes_same_class_call_card_again() -> None:
    """④ grant 撤销后同类调用重弹卡（撤销即时生效）。"""
    hi, _policy, _ledger_cap, ledger = _wire(["approved_remember"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG})

    await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))  # 授卡并双写 grant
    grant_id = ledger.list(subject_kind="pipeline", subject_id="p1")[0]["grant_id"]
    assert ledger.revoke(grant_id=grant_id) is True

    hi.answers.append("approved_once")
    third = await plugin.execute(_ctx(calls=_file_write_call("D:/out/c.txt")))
    assert len(hi.cards) == 2, "revoke 后同类调用重弹卡"
    assert _blocked(third) is False, "重弹卡经批准放行"


# ── ⑤ grants 面不可用/异常 → inert 放行 + grants_degraded 一次性横幅 ────────


@pytest.mark.asyncio
async def test_ledger_service_error_passes_with_degraded_banner() -> None:
    """⑤a authorize 服务异常：等价 grants 面不存在——inert 放行 + 横幅一次，零弹卡。"""
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    try:
        hi, _policy, ledger_cap, _ledger = _wire([], ledger_fail=True)
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG}
        )
        result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    finally:
        _mod.set_frontend_emit(None)

    assert hi.cards == [], "账本异常 → 不弹卡（既有审批语义零变化，用户裁定 2026-11-19）"
    assert not _blocked(result), "inert 放行"
    assert ledger_cap is not None
    assert len(ledger_cap.ledger_calls("authorize")) == 1, "异常真实性：authorize 确实被调用并抛错"
    banners = recorder.of("grants_degraded")
    assert len(banners) == 1, f"降级横幅恰一次，实际 {len(banners)} 次"
    assert "既有审批语义" in banners[0][1].get("message", ""), "横幅文案说明按既有审批语义放行"
    assert banners[0][2] == "s1", "横幅按会话路由"


@pytest.mark.asyncio
async def test_policy_service_error_passes_with_degraded_banner() -> None:
    """⑤b classify 服务异常（default 档规则未命中）：inert 放行 + 横幅一次，零弹卡。"""
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    try:
        hi, policy_cap, _ledger_cap, _ledger = _wire([], policy_fail=True)
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG}
        )
        result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    finally:
        _mod.set_frontend_emit(None)

    assert hi.cards == [], "分级面异常 → 不弹卡（等价 grants 面不存在）"
    assert not _blocked(result), "inert 放行"
    assert policy_cap is not None
    assert policy_cap.classify_calls() == 1, "异常真实性：classify 确实被调用并抛错"
    banners = recorder.of("grants_degraded")
    assert len(banners) == 1, f"降级横幅恰一次，实际 {len(banners)} 次"


@pytest.mark.asyncio
async def test_auto_mode_classify_error_passes_with_banner() -> None:
    """auto 档 + classify 异常（规则未命中）→ 放行 + 横幅一次（老语义：黑名单未命中按档放行）。"""
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    try:
        hi, policy_cap, _ledger_cap, _ledger = _wire([], policy_fail=True)
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), "mode": "auto", **_FILE_WRITE_DANGEROUS_CONFIG}
        )
        result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    finally:
        _mod.set_frontend_emit(None)

    assert hi.cards == [], "auto 档异常 → 放行（老语义）"
    assert not _blocked(result)
    assert policy_cap is not None
    assert policy_cap.classify_calls() == 1, "异常确实发生"
    assert len(recorder.of("grants_degraded")) == 1, "降级横幅恰一次"


@pytest.mark.asyncio
async def test_default_mode_classify_error_passes_with_banner() -> None:
    """default 档 + classify 异常（规则未命中）→ 放行 + 横幅一次（老语义：黑名单未命中任何档直接执行）。"""
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    try:
        hi, policy_cap, _ledger_cap, _ledger = _wire([], policy_fail=True)
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), "mode": "default", **_FILE_WRITE_DANGEROUS_CONFIG}
        )
        result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    finally:
        _mod.set_frontend_emit(None)

    assert hi.cards == [], "default 档异常 + 规则未命中 → 放行（等价 grants 面不存在，黑名单制老语义）"
    assert not _blocked(result)
    assert policy_cap is not None
    assert policy_cap.classify_calls() == 1, "异常确实发生"
    assert len(recorder.of("grants_degraded")) == 1, "降级横幅恰一次"


@pytest.mark.asyncio
async def test_default_mode_rule_hit_still_cards_despite_grants_error() -> None:
    """default 档 + classify 异常 + 规则命中 → 仍按规则弹卡（规则面优先不受 grants 异常影响，无横幅）。"""
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    try:
        hi, _policy, _ledger_cap, _ledger = _wire(["approved_once"], policy_fail=True)
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), "mode": "default"}
        )
        result = await plugin.execute(_ctx(command="pip install requests"))
    finally:
        _mod.set_frontend_emit(None)

    assert len(hi.cards) == 1, "default 档规则命中（本来就该弹）不受 grants 异常影响"
    assert not _blocked(result), "弹卡经批准放行"
    assert recorder.of("grants_degraded") == [], "规则面处置不经 grants 分支，无降级横幅"


@pytest.mark.asyncio
async def test_error_storm_bypass_zero_cards_banner_once_all_pass() -> None:
    """错误风暴（R4 回归锁）：bypass 档全部调用 classify 异常 → 零弹卡 + 每管道横幅恰一次 + 全部放行。

    语义零变化证明：旁路档静默放行是既有路径（隔离管道默认免审批），grants
    异常不得逐调用弹卡——用户线上实测回归「启动后每个管道都在弹审批卡」的
    直接来源即本场景（bypass 档 error 逐调用弹卡）。"""
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    try:
        hi, policy_cap, _ledger_cap, _ledger = _wire([], policy_fail=True)
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), "mode": "bypass"}
        )
        calls = [
            {"name": "phone_control", "args": {"action": "click", "x": 1, "y": 2}},
            {"name": "wallet", "args": {"action": "pay", "amount": 1}},
            {"name": "bash", "args": {"command": "python script.py"}},
            {"name": "file_read", "args": {"path": "D:/out/a.txt"}},
        ]
        first = await plugin.execute(_ctx(calls=calls))
        second = await plugin.execute(_ctx(calls=calls))
        other_pipeline = await plugin.execute(_ctx(calls=calls, pipeline_id="p2"))
    finally:
        _mod.set_frontend_emit(None)

    assert hi.cards == [], "错误风暴零弹卡（旁路档既有静默放行语义）"
    assert not _blocked(first), "全部调用放行"
    assert not _blocked(second), "第二轮仍全部放行"
    assert not _blocked(other_pipeline), "异管道同样放行"
    assert policy_cap is not None
    assert policy_cap.classify_calls() == 12, "风暴真实性：4 调用 × 3 轮每调用都实际触发 classify 异常"
    p1_banners = [e for e in recorder.of("grants_degraded") if e[1].get("pipeline_id") == "p1"]
    p2_banners = [e for e in recorder.of("grants_degraded") if e[1].get("pipeline_id") == "p2"]
    assert len(p1_banners) == 1, f"p1 两轮 8 次异常横幅恰一次，实际 {len(p1_banners)} 次"
    assert len(p2_banners) == 1, f"p2 独立计一次横幅，实际 {len(p2_banners)} 次"


@pytest.mark.asyncio
async def test_degraded_banner_channel_missing_logs_only_and_passes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """frontend.emit 未注入：横幅降级为日志留痕，主流程照常 inert 放行（不阻断）。"""
    import logging

    # 夹具已把 frontend_emit 复位为 None——本测试不注入 recorder
    hi, _policy, _ledger_cap, _ledger = _wire([], policy_fail=True)
    plugin = _mod.SecurityCheckPlugin(
        config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG}
    )
    with caplog.at_level(logging.WARNING, logger="security_check_plugin_grants_wiring"):
        result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    assert hi.cards == [], "通道缺席同样不弹卡"
    assert not _blocked(result), "inert 放行不因通道缺席而阻断"
    warnings = [r for r in caplog.records if "grants 降级提示不推前端" in r.getMessage()]
    assert warnings, "通道缺席必须落 warning 日志（禁静默降级）"


@pytest.mark.asyncio
async def test_degraded_banner_emit_failure_does_not_block() -> None:
    """emit 发射失败：只留日志不阻断安全检查，调用照常 inert 放行。"""

    class _BoomEmit:
        async def __call__(self, _event: str, _payload: dict[str, Any], _thread_id: str) -> None:
            raise RuntimeError("frontend channel boom")

    _mod.set_frontend_emit(_BoomEmit())
    try:
        hi, _policy, _ledger_cap, _ledger = _wire([], policy_fail=True)
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG}
        )
        result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
        assert hi.cards == [], "发射失败同样不弹卡"
        assert not _blocked(result), "inert 放行不因发射失败而阻断"
    finally:
        _mod.set_frontend_emit(None)


@pytest.mark.asyncio
async def test_auto_class_unaffected_by_service_error() -> None:
    """⑤c auto 类照常：classify 成功给 auto 后账本异常不影响放行（auto 零账本依赖）。"""
    hi, _policy, ledger_cap, _ledger = _wire([], ledger_fail=True)
    config = {
        "enabled": True,
        "rules": _rules(),
        "builtin_tools_config": {
            "tools": [{"name": "file_read", "dangerous_operations": ["read:/data/"]}]
        },
    }
    plugin = _mod.SecurityCheckPlugin(config=config)

    result = await plugin.execute(_ctx(calls=[{"name": "file_read", "args": {"path": "/data/n.txt"}}]))
    assert not _blocked(result), "auto 类照常放行"
    assert hi.cards == []
    assert ledger_cap is not None
    assert ledger_cap.calls == [], "auto 类不触碰账本（异常面无关）"


# ── ⑥ final_approval 规则不受任何 grant 影响 ───────────────────────────────


@pytest.mark.asyncio
async def test_final_approval_rule_immune_to_grants() -> None:
    """⑥ 预置 grant 在账，final_approval 规则命中仍逐次人审（终审标注在卡面）。"""
    hi, _policy, _ledger_cap, ledger = _wire(["approved_once"])
    rules = [
        *_rules(),
        {
            "name": "final_approval_mass_delete",
            "final_approval": True,
            "tools": ["*"],
            "params": ["command", "cmd"],
            "action": "needs_approval",
            "patterns": [{"type": "regex", "value": r"\brm\s+-[A-Za-z]*[rR][A-Za-z]*f"}],
        },
    ]
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": rules})
    # 预置 bash.execute task grant（本管道主体）——终审不得被任何 grant 豁免
    ledger.request(
        subject_kind="pipeline",
        subject_id="p1",
        tool="bash",
        action="execute",
        granularity="task",
        source="approval_card",
    )

    result = await plugin.execute(_ctx(command="rm -rf /tmp/x"))
    assert _blocked(result) is False, "终审卡经批准放行"
    assert len(hi.cards) == 1, "final_approval 规则命中仍逐次人审（grant 在账不豁免）"
    row = next(g for g in ledger.list(subject_kind="pipeline", subject_id="p1") if g["tool"] == "bash")
    assert row["uses"] == 0, "终审调用不得消费 grant"


# ── ⑦ 旁路档下 device_control/financial 类仍逐次人审 ───────────────────────


@pytest.mark.asyncio
async def test_bypass_mode_device_control_still_per_call_approval() -> None:
    """⑦a 旁路档：device_control 类（phone_control.click）仍走逐次人审。"""
    hi, _policy, _ledger_cap, _ledger = _wire(["approved_once"])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), "mode": "bypass"})

    result = await plugin.execute(_ctx(calls=[
        {"name": "phone_control", "args": {"action": "click", "x": 10, "y": 20}},
    ]))
    assert len(hi.cards) == 1, "旁路档不免 device_control 类逐次人审（ADR D3）"
    assert _blocked(result) is False, "经批准放行"


@pytest.mark.asyncio
async def test_bypass_mode_financial_class_still_per_call_approval() -> None:
    """⑦b 旁路档：financial 类 verdict 仍走逐次人审（分级面覆盖注入）。"""
    hi, _policy, _ledger_cap, _ledger = _wire(["approved_once"], policy_overrides={
        "wallet.pay": {
            "verdict": "per_call_approval",
            "effect": "financial",
            "scope": None,
            "policy_key": "wallet.pay",
            "reason": None,
        }
    })
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), "mode": "bypass"})

    result = await plugin.execute(_ctx(calls=[
        {"name": "wallet", "args": {"action": "pay", "amount": 1}},
    ]))
    assert len(hi.cards) == 1, "旁路档不免 financial 类逐次人审"
    assert _blocked(result) is False


@pytest.mark.asyncio
async def test_bypass_mode_low_risk_classes_pass_silently() -> None:
    """⑦c 旁路档免审批摩擦保留：task_grant/auto 类照常静默放行（仅高风险类闸）。"""
    hi, _policy, _ledger_cap, _ledger = _wire([])
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), "mode": "bypass"})

    result = await plugin.execute(_ctx(command="python script.py", calls=[
        {"name": "bash", "args": {"command": "python script.py"}},
        {"name": "file_read", "args": {"path": "D:/out/a.txt"}},
    ]))
    assert hi.cards == [], "旁路档 task_grant/auto 类不弹卡（免审批摩擦语义不变）"
    assert not _blocked(result)


@pytest.mark.asyncio
async def test_bypass_mode_without_grants_caps_stays_friction_free() -> None:
    """⑦d 账本面缺席（未注入）时旁路档维持既有免审批语义（inert 降级，不新增卡）。"""
    hi, _policy, _ledger_cap, _ledger = _wire([], with_grants=False)
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), "mode": "bypass"})

    result = await plugin.execute(_ctx(calls=[
        {"name": "phone_control", "args": {"action": "click", "x": 1, "y": 2}},
    ]))
    assert hi.cards == [], "grants 面缺席 → 旁路档按既有语义放行（不新增卡）"
    assert not _blocked(result)


# ── 分支级直测（diff coverage 清偿）：deny/per_call 分流、降级面、动作键派生 ──


@pytest.mark.asyncio
async def test_grants_dispatch_deny_blocks_without_card() -> None:
    """规则未覆盖 + 分级 deny → 拦截（软拦截直出，不建审批卡）。"""
    hi, _policy, _ledger_cap, _ledger = _wire([], policy_overrides={
        "file_write.write": {
            "verdict": "deny", "effect": "write", "scope": None,
            "policy_key": "file_write.write", "reason": None,
        }
    })
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG})

    result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    assert hi.cards == [], "deny 分级拦截不建审批卡"
    assert _blocked(result), "deny → pre_decided_results 拦截"


@pytest.mark.asyncio
async def test_grants_dispatch_per_call_approval_cards_non_high_risk_effect() -> None:
    """规则未覆盖 + 分级 per_call_approval（write effect，非高风险类）→ 既有审批卡逐次。"""
    hi, _policy, _ledger_cap, _ledger = _wire(["approved_once"], policy_overrides={
        "file_write.write": {
            "verdict": "per_call_approval", "effect": "write", "scope": None,
            "policy_key": "file_write.write", "reason": None,
        }
    })
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG})

    result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
    assert len(hi.cards) == 1, "per_call_approval 分级逐次弹卡（既有 needs_approval 路径）"
    assert not _blocked(result)


@pytest.mark.asyncio
async def test_bypass_mode_policy_error_stays_friction_free_with_banner() -> None:
    """旁路档 + 分级面异常 → 零弹卡静默放行 + 降级横幅一次（既有免审批语义零变化）。"""
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    try:
        hi, policy_cap, _ledger_cap, _ledger = _wire([], policy_fail=True)
        plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), "mode": "bypass"})
        result = await plugin.execute(_ctx(calls=[
            {"name": "phone_control", "args": {"action": "click", "x": 1, "y": 2}},
        ]))
    finally:
        _mod.set_frontend_emit(None)

    assert hi.cards == [], "旁路档异常 → 不弹卡（免审批摩擦语义零变化）"
    assert not _blocked(result)
    assert policy_cap is not None
    assert policy_cap.classify_calls() == 1, "异常确实发生"
    assert len(recorder.of("grants_degraded")) == 1, "降级横幅恰一次"


@pytest.mark.asyncio
async def test_bypass_mode_deny_class_blocks_without_card() -> None:
    """旁路档 + 分级 deny → 恒拒（软拦截直出，不建卡）。"""
    hi, _policy, _ledger_cap, _ledger = _wire([], policy_overrides={
        "phone_control.click": {
            "verdict": "deny", "effect": "device_control", "scope": None,
            "policy_key": "phone_control.click", "reason": None,
        }
    })
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), "mode": "bypass"})

    result = await plugin.execute(_ctx(calls=[
        {"name": "phone_control", "args": {"action": "click", "x": 1, "y": 2}},
    ]))
    assert hi.cards == [], "deny 恒拒不建卡"
    assert _blocked(result), "旁路档 deny 也拦截"


def test_grants_action_key_falls_back_when_policy_loader_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """动作键派生：隔离策略解析失败 → 无动作位（整工具键回落）。"""
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True})

    class _BoomLoader:
        @staticmethod
        def resolve(_name: str) -> Any:
            raise RuntimeError("isolation policy loader boom")

    monkeypatch.setattr(_mod, "_policy_loader", _BoomLoader())
    assert plugin._grants_action_key("bash", {"command": "ls"}) == ""
    assert plugin._grants_action_key("file_read", {}) == ""


@pytest.mark.asyncio
async def test_grants_classify_garbage_verdict_is_error() -> None:
    """分级返回畸形形态（无 verdict/非 dict）→ error（调用方 inert 放行 + 一次性降级横幅）。"""

    class _GarbageCap:
        async def call(self, _method: str, _params: dict[str, Any], **_kw: Any) -> Any:
            return {"unexpected": "shape"}

    status, verdict = await _mod.SecurityCheckPlugin._grants_classify(_GarbageCap(), "file_write", "write")
    assert status == "error"
    assert verdict is None


@pytest.mark.asyncio
async def test_grants_issue_and_consume_degrade_on_ledger_rejection_and_error() -> None:
    """双写/消费的账本被拒与异常面：留痕不回滚、不阻断审批主流程。"""
    hi, _policy, ledger_cap, ledger = _wire(["approved_remember"], reject_request=True)
    assert ledger_cap is not None, "ledger cap 须在位"
    plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG})
    ctx = _ctx(calls=_file_write_call("D:/out/a.txt"))

    # issue 被拒（ok=False）与异常两形态均只留日志
    await plugin._grants_issue(ctx, tool="file_write", action="write", granularity="task",
                               subject_kind="pipeline", subject_id="p1", evidence="req-1", pipeline_id="p1")
    ledger_cap.reject_request = False
    ledger_cap.fail = True
    await plugin._grants_issue(ctx, tool="file_write", action="write", granularity="task",
                               subject_kind="pipeline", subject_id="p1")
    ledger_cap.fail = False
    # consume 被拒与异常两形态
    await plugin._grants_consume({"grant_id": "unknown-grant"}, tool_name="file_write", action_key="write")
    ledger_cap.consume_fail = True
    await plugin._grants_consume({"grant_id": "g1"}, tool_name="file_write", action_key="write")
    ledger_cap.consume_fail = False
    # 账本面缺席（cap None）双早退
    _mod.set_grants_caps(None, None)
    await plugin._grants_issue(ctx, tool="x", action="", granularity="task", subject_kind="pipeline", subject_id="p")
    await plugin._grants_consume({"grant_id": "g"}, tool_name="x", action_key="")
    assert ledger.list(subject_kind="pipeline") == [], "request 被拒/异常均不落账本行"


@pytest.mark.asyncio
async def test_ledger_cap_absent_with_policy_ok_passes_with_banner() -> None:
    """分级面在而账本面缺席（混合可用性）→ 等价 grants 面不存在：inert 放行 + 横幅一次。"""
    assert _STATE_DIR is not None
    recorder = _EmitRecorder()
    _mod.set_frontend_emit(recorder)
    policy_cap = FakePolicyCap(_engine())
    hi = FakeHiCap([])
    _mod.set_human_interaction_cap(hi)
    _mod.set_grants_caps(policy_cap, None)
    try:
        plugin = _mod.SecurityCheckPlugin(
            config={"enabled": True, "rules": _rules(), **_FILE_WRITE_DANGEROUS_CONFIG}
        )
        result = await plugin.execute(_ctx(calls=_file_write_call("D:/out/a.txt")))
        assert hi.cards == [], "账本面缺席 → 不弹卡（既有审批语义零变化）"
        assert not _blocked(result), "inert 放行"
        assert policy_cap.classify_calls() == 1, "分级面在位且完成 classify"
    finally:
        _mod.set_grants_caps(None, None)
        _mod.set_frontend_emit(None)
    banners = recorder.of("grants_degraded")
    assert len(banners) == 1, f"降级横幅恰一次，实际 {len(banners)} 次"
