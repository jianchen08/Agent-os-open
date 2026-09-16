# -*- coding: utf-8 -*-
# @feature: FP-0.2.二 eval_harness 缺口分支补测 | @ci: python-coverage
"""eval_harness 单元测试（不依赖内核）：题集展开/聚合/提案校验/账本/模式服务解析。"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))

import aggregate  # noqa: E402
import proposal as proposal_mod  # noqa: E402
import suite_reader  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _load_server_module():
    """按唯一模块名装载本插件 server.py（避开车道内裸名 server 的跨插件互覆）。"""
    path = os.path.join(os.path.dirname(__file__), "server.py")
    spec = importlib.util.spec_from_file_location("eval_harness_service_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _valid_proposal(target="config/agents/main/task_dispatch_guide.md",
                    base_hash="", change_type="content_edit",
                    content="示例内容", layer="L1") -> dict:
    return {"layer": layer, "target": target, "change_type": change_type,
            "content": content, "motivation": "签名簇 S1", "base_hash": base_hash}


# ── 题集展开 ─────────────────────────────────────────────────────────────
def _write_suite(tmp_path, cases):
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"name": "t", "mode": "coding", "cases": cases},
                                   allow_unicode=True), encoding="utf-8")
    return str(path)


def test_expand_batches_shape(tmp_path):
    suite = _write_suite(tmp_path, [
        {"id": "c1", "category": "normal", "target": "executor/general_agent",
         "messages": ["做 A"],
         "acceptance_criteria": {"semantic_check": {"input_params": {"criteria": "x"}}}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), run_tag="T1")
    args = batches[0]["task_submit_args"]
    assert args["target_type"] == "agent"
    assert args["target_id"] == "executor/general_agent"
    assert args["goal_title"] == "eval-c1-T1"
    assert args["goal_description"] == "做 A"
    assert "semantic_check" in args["acceptance_criteria"]


def test_expand_truncates_long_goal(tmp_path):
    suite = _write_suite(tmp_path, [
        {"id": "c2", "messages": ["字" * 3000],
         "acceptance_criteria": {"semantic_check": {"input_params": {"criteria": "x"}}}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), run_tag="T")
    assert len(batches[0]["task_submit_args"]["goal_description"]) <= 2000


def test_expand_case_filter(tmp_path):
    suite = _write_suite(tmp_path, [
        {"id": "a", "messages": ["x"], "acceptance_criteria": {}},
        {"id": "b", "messages": ["y"], "acceptance_criteria": {}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), cases="b")
    assert [b["case_id"] for b in batches] == ["b"]


# ── 模式 profile → 题集解析（profile 经 mode.get_profile 服务调用获取） ──
def test_resolve_suite_from_profile_picks_tier():
    profile = {"mode": "coding", "suite": {"smoke": "config/s Smoke.yaml", "full": None}}
    assert suite_reader.resolve_suite_from_profile(profile) == "config/s Smoke.yaml"


@pytest.mark.parametrize("profile,tier,fragment", [
    ({"mode": "x", "suite": {"smoke": "a.yaml", "full": None}}, "full", "full"),
    ({"mode": "x", "suite": {}}, "smoke", "缺失"),
    ({"mode": "x"}, "smoke", "缺失"),
    ({"mode": "x", "suite": {"smoke": 42}}, "smoke", "未登记"),
])
def test_resolve_suite_rejects_malformed_profile(profile, tier, fragment):
    with pytest.raises(ValueError, match=fragment):
        suite_reader.resolve_suite_from_profile(profile, tier)


def _write_service_suite(root, name, case_id):
    """真实题集文件落盘（文件 IO 走真实路径，服务调用边界才允许 mock）。"""
    rel = f"suites/{name}.yaml"
    path = root / "suites" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "mode": name,
        "cases": [{"id": case_id, "messages": ["做 A"],
                   "acceptance_criteria": {"semantic_check": {"input_params": {"criteria": "x"}}}}],
    }, allow_unicode=True), encoding="utf-8")
    return rel


def test_view_suite_resolves_mode_via_service(tmp_path, monkeypatch):
    """mode → 服务取 profile → 题集展开：服务调用是外部边界（mock），题集文件走真实盘。"""
    server = _load_server_module()
    smoke_rel = _write_service_suite(tmp_path, "smoke", "c1")
    full_rel = _write_service_suite(tmp_path, "full", "c2")

    async def fake_fetch(mode):
        return {"mode": mode, "suite": {"smoke": smoke_rel, "full": full_rel}}

    monkeypatch.setattr(server, "_fetch_mode_profile", fake_fetch)
    monkeypatch.setattr(server, "_PROJECT_ROOT", str(tmp_path))
    smoke = asyncio.run(server.eval_view_suite(mode="coding"))
    assert smoke["success"] is True and smoke["suite"] == smoke_rel
    assert smoke["batches"][0]["case_id"] == "c1"
    # 第二组区分度输入：同模式不同档位解析到不同题集
    full = asyncio.run(server.eval_view_suite(mode="coding", tier="full"))
    assert full["batches"][0]["case_id"] == "c2"


def test_fetch_mode_profile_rejects_invalid_envelope(tmp_path, monkeypatch):
    """服务返回失败信封/无效载荷 → fetcher 抛 ValueError（tool 层翻译为错误值）。"""
    server = _load_server_module()

    async def failing_invoke(method, params, timeout=None):
        return {"success": False, "error": "service down"}

    monkeypatch.setattr(server, "_tool_executor_caller", lambda: failing_invoke)
    with pytest.raises(ValueError, match="无效 profile"):
        asyncio.run(server._fetch_mode_profile("coding"))


@pytest.mark.parametrize("envelope", [
    {"data": {"mode": "coding", "suite": {"smoke": "a.yaml"}}},   # 调用信封形态
    {"mode": "coding", "suite": {"smoke": "a.yaml"}},             # profile 本体形态
])
def test_fetch_mode_profile_unwraps_both_envelopes(monkeypatch, envelope):
    """信封两种形态（{"data": …} / profile 本体）都解包出 profile。"""
    server = _load_server_module()

    async def fake_invoke(method, params, timeout=None):
        return envelope

    monkeypatch.setattr(server, "_tool_executor_caller", lambda: fake_invoke)
    profile = asyncio.run(server._fetch_mode_profile("coding"))
    assert profile["mode"] == "coding"
    assert profile["suite"] == {"smoke": "a.yaml"}


def test_fetch_mode_profile_calls_mode_service(tmp_path, monkeypatch):
    """调用形状：tool-executor.invoke + 显式 plugin_id=mode_<mode> + tool_name=mode.get_profile。"""
    server = _load_server_module()
    captured = {}

    async def fake_invoke(method, params, timeout=None):
        captured["method"] = method
        captured["params"] = params
        return {"data": {"mode": "coding", "suite": {"smoke": "a.yaml"}}}

    monkeypatch.setattr(server, "_tool_executor_caller", lambda: fake_invoke)
    profile = asyncio.run(server._fetch_mode_profile("coding"))
    assert profile["mode"] == "coding"
    assert captured["method"] == "tool-executor.invoke"
    assert captured["params"] == {"tool_name": "mode.get_profile",
                                  "plugin_id": "mode_coding", "args": {}}


class _FakeToolExecutorCap:
    """tool-executor 能力句柄桩（内核连接是外部边界）：记录 invoke 参数并回放任务 id。"""

    def __init__(self, responder):
        self._respond = responder

    async def call(self, method, params, timeout=None):
        return await self._respond(method, params, timeout)


class _FakePlugin:
    def __init__(self, responder):
        self._responder = responder

    def get_capability(self, name):
        assert name == "tool-executor"
        return _FakeToolExecutorCap(self._responder)


def _write_heldout_suite(tmp_path, mode, case_ids):
    (tmp_path / f"{mode}.yaml").write_text(yaml.safe_dump(
        {"mode": mode,
         "cases": [{"id": cid, "messages": [f"题面 {cid}"],
                    "acceptance_criteria": {}} for cid in case_ids]},
        allow_unicode=True), encoding="utf-8")


def test_heldout_dispatch_keeps_prompts_internal(tmp_path, monkeypatch):
    """held-out 保密派发：内部经 tool-executor 派发任务，只回 case↔task 映射。"""
    server = _load_server_module()
    _write_heldout_suite(tmp_path, "coding", ["h1", "h2"])
    monkeypatch.setenv("AGENTOS_HELDOUT_DIR", str(tmp_path))
    calls = []

    async def respond(method, params, timeout=None):
        calls.append((method, params))
        return {"data": {"task_id": f"t-{params['args']['goal_title']}"}}

    monkeypatch.setattr(server, "plugin", _FakePlugin(respond))
    out = asyncio.run(server.eval_run_heldout(mode="coding"))
    assert out["success"] is True
    assert {d["case_id"] for d in out["dispatched"]} == {"h1", "h2"}
    assert all(d["task_id"].startswith("t-heldout-") for d in out["dispatched"])
    # 派发走 task_submit（工具名按 invoke 契约传输，前缀已由 caller 剥离）
    assert calls and calls[0][0] == "invoke"
    assert calls[0][1]["tool_name"] == "task_submit"
    assert calls[0][1]["plugin_id"] == "task_submit_tool"


def test_heldout_malformed_suite_reports_error(tmp_path, monkeypatch):
    server = _load_server_module()
    (tmp_path / "coding.yaml").write_text("cases: []\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_HELDOUT_DIR", str(tmp_path))
    out = asyncio.run(server.eval_run_heldout(mode="coding"))
    assert out["success"] is False
    assert "题集" in out["error"]


def test_view_suite_translates_service_error(tmp_path, monkeypatch):
    """服务不可用 → 工具如实报错（错误翻译为值，不吞不抛）。"""
    server = _load_server_module()

    async def fake_fetch(mode):
        raise ValueError("mode.get_profile 返回无效 profile: service down")

    monkeypatch.setattr(server, "_fetch_mode_profile", fake_fetch)
    out = asyncio.run(server.eval_view_suite(mode="coding"))
    assert out["success"] is False
    assert "无效 profile" in out["error"]


def test_view_suite_real_coding_seed_profile():
    """真实依赖全链（零 mock）：mode_coding 出厂种子 profile → smoke 题集真实存在且可展开。"""
    with open(os.path.join(ROOT, "plugins/shared/modes/mode_coding/profile.yaml"),
              encoding="utf-8") as fh:
        profile = yaml.safe_load(fh)
    suite_rel = suite_reader.resolve_suite_from_profile(profile, "smoke")
    suite = suite_reader.load_suite(os.path.join(ROOT, suite_rel))
    batches = suite_reader.expand_batches(suite, run_tag="T1")
    assert batches and all("task_submit_args" in b for b in batches)


# ── 聚合与失败分类 ───────────────────────────────────────────────────────
def test_summarize_pass_and_fail():
    results = [
        {"case_id": "c1", "task_status": "completed",
         "criteria": {"semantic_check": True}},
        {"case_id": "c2", "task_status": "completed",
         "criteria": {"semantic_check": False}},
        {"case_id": "c3", "task_status": "failed", "criteria": {}},
    ]
    out = aggregate.summarize("coding", results, baseline_passed=2)
    assert out["passed"] == 1 and out["total"] == 3
    assert out["delta"] == -1
    by_id = {r["case_id"]: r for r in out["results"]}
    assert by_id["c2"]["behavior_class"] == ["acceptance_miss"]
    assert by_id["c3"]["behavior_class"] == ["task_failure"]


# ── budget_ok 谓词（ADR 2026-09-16：终态∧AC∧budget_ok） ─────────────────
def test_budget_blowout_blocks_pass_even_when_criteria_pass():
    """终态 + AC 全过但超预算 → 不计通过，标 budget_blowout。"""
    results = [{"case_id": "c1", "task_status": "completed",
                "criteria": {"file_check": True},
                "budget": {"max_rounds": 5, "max_tokens": 100000},
                "metrics": {"rounds": 9, "tokens": 5000}}]
    out = aggregate.summarize("coding", results)
    assert out["passed"] == 0
    assert out["results"][0]["behavior_class"] == ["budget_blowout"]


def test_budget_declared_without_metrics_flags_unverified_not_fail():
    """声明预算但无 metrics 采集 → 不判死（budget_unverified），向后兼容。"""
    results = [{"case_id": "c1", "task_status": "completed",
                "criteria": {"file_check": True},
                "budget": {"max_rounds": 5}}]
    out = aggregate.summarize("coding", results)
    assert out["passed"] == 1
    assert out["results"][0]["budget_unverified"] is True


def test_budget_within_limit_and_absent_budget_pass():
    """预算内通过；无预算声明行为与旧口径完全一致（≥2 组区分输入）。"""
    within = aggregate.summarize("coding", [{
        "case_id": "c1", "task_status": "completed",
        "criteria": {"file_check": True},
        "budget": {"max_rounds": 40}, "metrics": {"rounds": 12}}])
    assert within["passed"] == 1 and "budget_unverified" not in within["results"][0]
    legacy = aggregate.summarize("coding", [{
        "case_id": "c2", "task_status": "completed",
        "criteria": {"file_check": True}}])
    assert legacy["passed"] == 1


# ── 提案校验（冻结面含安全红线文件） ─────────────────────────────────────
def test_validate_pass_on_legal_proposal():
    assert proposal_mod.validate(_valid_proposal(), ROOT) == []


@pytest.mark.parametrize("target", [
    "config/self_evolve/rules/evolution_rules.md",
    "config/self_evolve/suites/dev/smoke.yaml",
    "plugins/shared/system/eval_harness/server.py",
    "plugins/shared/modes/mode_coding/plugin.json",
    "plugins/shared/modes/mode_coding/profile.yaml",
    "kernel/crates/api/src/routes.rs",
    ".env",
    "config/rules/information_integrity_rules.md",
    # 裁决权载体（ADR 2026-09-16：被裁决者不可经提案改裁判）
    "config/plugins/evaluation/evaluation_metrics.yaml",
    "config/plugins/review/triage_rules.yaml",
    "plugins/shared/system/evaluation/plugin.py",
    "plugins/shared/system/review/improvement.py",
])
def test_validate_rejects_frozen_paths(target):
    violations = proposal_mod.validate(_valid_proposal(target=target), ROOT)
    assert any(v.startswith("frozen:") for v in violations), target


def test_validate_rejects_outside_whitelist_and_escape():
    assert any(v.startswith("whitelist:")
               for v in proposal_mod.validate(_valid_proposal(target="docs/x.md"), ROOT))
    assert any("逃逸" in v
               for v in proposal_mod.validate(_valid_proposal(target="../../etc/p"), ROOT))


def test_validate_rejects_base_hash_mismatch():
    violations = proposal_mod.validate(_valid_proposal(base_hash="deadbeef"), ROOT)
    assert any(v.startswith("base:") for v in violations)


# ── 账本（reports/eval 下） ─────────────────────────────────────────────
def test_ledger_roundtrip(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setattr("builtins.open", open)  # no-op 保持可读性
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    # server 模块的 _EVAL_REPORTS 在 import 时按项目根定位；此处直接验证聚合文件语义
    summary_path = tmp_path / "reports" / "eval" / "r1" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps({"run_id": "r1", "passed": 1}), encoding="utf-8")
    loaded = json.load(open(summary_path, encoding="utf-8"))
    assert loaded["run_id"] == "r1"


# ── 自进化面板页（webview tab，插件自带） ────────────────────────────────
PANEL_ENDPOINT = "/ext/eval_harness_service/page/evolution-panel"
SNAPSHOT_ANCHOR = "/*__SNAPSHOT_INJECT__*/"

_PANEL_LEDGER = {
    "rounds": [
        {"run_id": "r1", "mode": "coding", "passed": 2, "total": 4,
         "failed_cases": ["c3", "c4"]},
        {"run_id": "r2", "mode": "coding", "passed": 3, "total": 4,
         "failed_cases": ["c4"]},
        {"run_id": "r3", "mode": "writing", "passed": 1, "total": 2,
         "failed_cases": ["w2"]},
    ],
    "proposals": {
        "p1": {"outcome": "applied", "target": "config/agents/main/persona/a.md",
               "motivation": "签名簇 S1"},
        "p2": {"outcome": "rejected", "target": "config/agents/main/persona/b.md",
               "motivation": "签名簇 S2", "reason": "held-out 不过"},
    },
}


# ── 面板数据快照（账本投影；有账本/空账本两态，口径与 eval_health 同源） ──
def test_panel_snapshot_groups_rounds_by_mode_with_baseline_delta():
    snap = aggregate.panel_snapshot(_PANEL_LEDGER)
    assert snap["rounds_total"] == 3
    coding = snap["modes"]["coding"]
    assert coding["rounds"] == 2 and coding["passed"] == 5 and coding["total"] == 8
    # 基线 = 该模式首轮通过数，各轮 delta 相对基线（scorecard 走势数据）
    assert [p["delta_vs_baseline"] for p in coding["series"]] == [0, 1]
    assert snap["modes"]["writing"]["series"][0]["failed_cases"] == ["w2"]


def test_panel_snapshot_projects_proposals_and_lever_health():
    snap = aggregate.panel_snapshot(_PANEL_LEDGER)
    assert snap["proposals_total"] == 2 and snap["proposals_applied"] == 1
    assert snap["proposals_rejected"] == 1
    assert snap["hit_rate"] == 0.5 and snap["reject_rate"] == 0.5
    assert snap["levers"]["签名簇 S1"] == {"applied": 1, "total": 1}
    assert {h["proposal_id"] for h in snap["hypotheses"]} == {"p1", "p2"}


def test_panel_snapshot_empty_ledger_all_placeholder_sources():
    snap = aggregate.panel_snapshot({})
    assert snap["modes"] == {} and snap["rounds_total"] == 0
    assert snap["hypotheses"] == []
    assert snap["hit_rate"] is None and snap["reject_rate"] is None
    assert snap["breaker"] is None  # 熔断未落账 → 页面按占位渲染
    assert snap["evolution"]["verdict"] == "insufficient_data"  # 空账不猜测


def test_panel_snapshot_embeds_p0p3_verdict():
    """快照携带 P0-P3 裁决（有正学习率与达标增益 → evolving，P3 无否决）。"""
    snap = aggregate.panel_snapshot(_PANEL_LEDGER)
    evo = snap["evolution"]
    assert evo["verdict"] == "evolving"
    assert evo["p0"]["learning_rates"]["coding"]["slope"] > 0
    assert evo["p3"]["user_veto_reverts"] == 0


def test_panel_snapshot_proposal_without_motivation_has_no_lever():
    """无动机提案计入提案总数但不产杠杆条目（health_summary 既有口径）。"""
    ledger = {"rounds": [],
              "proposals": {"p1": {"outcome": "submitted", "target": "t",
                                   "motivation": ""},
                            "p2": {"outcome": "applied", "target": "t2"}}}
    snap = aggregate.panel_snapshot(ledger)
    assert snap["proposals_total"] == 2 and snap["proposals_applied"] == 1
    assert snap["hit_rate"] == 0.5 and snap["reject_rate"] == 0.0
    assert snap["levers"] == {}


def test_eval_health_shares_panel_aggregation(monkeypatch):
    """eval_health 工具与面板快照同一聚合口径（health_summary 同源复用）。"""
    server = _load_server_module()
    monkeypatch.setattr(server, "_load_ledger", lambda: _PANEL_LEDGER)
    out = asyncio.run(server.eval_health())
    assert out["success"] is True and out["rounds"] == 3
    health = aggregate.health_summary(_PANEL_LEDGER)
    assert out["hit_rate"] == health["hit_rate"]
    assert out["levers"] == health["levers"]


# ── http.handle 路由（面板页供给） ───────────────────────────────────────
def _decode_panel(result: dict) -> tuple[dict, str]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return data, base64.b64decode(data["body"]).decode("utf-8")


def test_panel_page_served_as_html_with_embedded_snapshot(monkeypatch):
    server = _load_server_module()
    monkeypatch.setattr(server, "_load_ledger", lambda: _PANEL_LEDGER)
    data, html = _decode_panel(asyncio.run(
        server.http_handle(path=PANEL_ENDPOINT, method="GET")))
    assert data["status"] == 200
    assert data["headers"]["Content-Type"] == "text/html; charset=utf-8"
    assert html.startswith("<!DOCTYPE html>")
    # 快照已内联：注入锚点被账本数据替换，且未引入额外脚本段（防 </script> 越界）
    assert SNAPSHOT_ANCHOR not in html
    assert "签名簇 S1" in html
    assert html.lower().count("<script") == 1
    # 信息架构锚点（设计稿 §3.5：四模式分组 + 下钻六区块 + T1-T8 状态机）
    for marker in ("轮次时间线", "scorecard 走势", "假设清单", "杠杆命中率",
                   "健康度五指标", "熔断状态", "编码", "写作", "角色扮演", "调研",
                   "T1 测量", "T8 循环决策", "自进化指标", "P0-P3"):
        assert marker in html, f"缺信息架构区块 {marker}"


def test_panel_page_empty_ledger_serves_placeholder_snapshot(monkeypatch):
    server = _load_server_module()
    monkeypatch.setattr(server, "_load_ledger", lambda: {})
    data, html = _decode_panel(asyncio.run(
        server.http_handle(path=PANEL_ENDPOINT, method="GET")))
    assert data["status"] == 200
    assert '"rounds_total": 0' in html and '"hypotheses": []' in html
    assert SNAPSHOT_ANCHOR not in html


@pytest.mark.parametrize("kind", ["unknown_path", "wrong_method"])
def test_panel_unrouted_request_returns_404(kind):
    server = _load_server_module()
    if kind == "unknown_path":
        result = asyncio.run(server.http_handle(
            path="/ext/eval_harness_service/page/does-not-exist", method="GET"))
    else:
        result = asyncio.run(server.http_handle(path=PANEL_ENDPOINT, method="POST"))
    assert result["success"] is True
    data = result["data"]
    assert data["status"] == 404
    assert data["headers"]["Content-Type"].startswith("application/json")
    assert "error" in json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def test_panel_html_read_failure_returns_500(monkeypatch):
    """面板页文件缺失（OSError）→ 500 JSON 错误响应（边界翻译，不静默吐空页）。"""
    server = _load_server_module()
    monkeypatch.setattr(
        server, "_PANEL_HTML_PATH",
        os.path.join(os.path.dirname(__file__), "no_such_panel.html"))
    result = asyncio.run(server.http_handle(path=PANEL_ENDPOINT, method="GET"))
    assert result["success"] is False
    assert "panel html serve failed" in result["error"]
    data = result["data"]
    assert data["status"] == 500
    assert data["headers"]["Content-Type"].startswith("application/json")


def test_panel_missing_snapshot_anchor_fails_closed(tmp_path, monkeypatch):
    """包内页面被改坏（缺注入锚点）→ 500，不吐无快照页假成功。"""
    server = _load_server_module()
    bad = tmp_path / "bad_panel.html"
    bad.write_text("<!DOCTYPE html><html><body>x</body></html>", encoding="utf-8")
    monkeypatch.setattr(server, "_PANEL_HTML_PATH", str(bad))
    monkeypatch.setattr(server, "_load_ledger", lambda: {})
    result = asyncio.run(server.http_handle(path=PANEL_ENDPOINT, method="GET"))
    assert result["success"] is False
    assert "锚点" in result["error"]
    assert result["data"]["status"] == 500


def test_panel_html_is_self_contained():
    """宿主 CSP（default-src 'none' + connect-src 'none'）约束面板页自包含：
    禁外链资源、禁网络拉取；数据经供页内联快照；深浅色走 CSS 变量。"""
    path = os.path.join(os.path.dirname(__file__), "webview", "evolution_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    lowered = html.lower()
    for banned in ("src=", "href=", "http://", "https://", "fetch(", "url(",
                   "websocket", "import("):
        assert banned not in lowered, f"面板页含被 CSP 禁止的引用 {banned}"
    assert "prefers-color-scheme" in lowered
    assert SNAPSHOT_ANCHOR in html  # 供页注入锚点在包内（自包含 + 服务端注数）


def test_manifest_declares_webview_tab_and_endpoint():
    """contributes.pages tab 声明 + http_endpoints 页面路由
    （形态对齐 monitoring payload_diag / mode_* 面板页先例）。"""
    with open(os.path.join(os.path.dirname(__file__), "plugin.json"),
              encoding="utf-8") as fh:
        manifest = json.load(fh)
    page = {p["id"]: p for p in manifest["contributes"]["pages"]}["evolution_panel"]
    assert page["title"] == "自进化"
    assert page["widget"] == "webview"
    assert page["space"] == "workspace" and page["slot"] == "tab"
    assert page["path"] == "/p/evolution_panel"
    # pluginId 与 manifest id 同名（G2 强制 /ext/{plugin_id}/** 命名空间，
    # 且 WebviewWidget 上行白名单按该前缀校验）
    assert page["props"] == {
        "pluginId": "eval_harness_service",
        "htmlPath": "/page/evolution-panel",
        "widgetId": "evolution_panel",
    }
    endpoint = {e["path"]: e for e in manifest["http_endpoints"]}[PANEL_ENDPOINT]
    assert endpoint["method"] == "GET"
    assert endpoint["auth"] == "user"
    assert endpoint["handler_capability"] == "http.handle"
    # timeout/并发对齐 monitoring 页面类路由（payload_diag/tool_calls 页 = 5000/4）
    assert endpoint["timeout_ms"] == 5000
    assert endpoint["max_concurrency"] == 4
