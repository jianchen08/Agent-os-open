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


def _validate(proposal: dict, root: str) -> list[str]:
    """validate 异步服务化（T6c）后的同步测试壳。"""
    return asyncio.run(proposal_mod.validate(proposal, root))

import suite_reader  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _load_server_module():
    """按唯一模块名装载本插件 server.py（避开车道内裸名 server 的跨插件互覆）。"""
    path = os.path.join(os.path.dirname(__file__), "server.py")
    spec = importlib.util.spec_from_file_location("eval_harness_service_server", path)
    assert spec is not None
    assert spec.loader is not None
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
    smoke = asyncio.run(server.eval_query(domain="suite", mode="coding"))
    assert smoke["success"] is True
    assert smoke["suite"] == smoke_rel
    assert smoke["batches"][0]["case_id"] == "c1"
    # 第二组区分度输入：同模式不同档位解析到不同题集
    full = asyncio.run(server.eval_query(domain="suite", mode="coding", tier="full"))
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
    assert calls
    assert calls[0][0] == "invoke"
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
    out = asyncio.run(server.eval_query(domain="suite", mode="coding"))
    assert out["success"] is False
    assert "无效 profile" in out["error"]


def test_view_suite_real_coding_seed_profile():
    """真实依赖全链（零 mock）：mode_coding 出厂种子 profile → smoke 题集真实存在且可展开。"""
    with open(os.path.join(ROOT, "plugins/shared/mode_coding/mode.yaml"),
              encoding="utf-8") as fh:
        profile = yaml.safe_load(fh)
    suite_rel = suite_reader.resolve_suite_from_profile(profile, "smoke")
    suite = suite_reader.load_suite(os.path.join(ROOT, suite_rel))
    batches = suite_reader.expand_batches(suite, run_tag="T1")
    assert batches
    assert all("task_submit_args" in b for b in batches)


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
    assert out["passed"] == 1
    assert out["total"] == 3
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
    assert within["passed"] == 1
    assert "budget_unverified" not in within["results"][0]
    legacy = aggregate.summarize("coding", [{
        "case_id": "c2", "task_status": "completed",
        "criteria": {"file_check": True}}])
    assert legacy["passed"] == 1


# ── 提案校验（冻结面含安全红线文件） ─────────────────────────────────────
def test_validate_pass_on_legal_proposal():
    assert _validate(_valid_proposal(), ROOT) == []


@pytest.mark.parametrize("target", [
    "plugins/shared/mode_evolution/rules/evolution_rules.md",
    "config/self_evolve/suites/dev/smoke.yaml",
    "plugins/shared/mode_evolution/server.py",
    "plugins/shared/mode_coding/plugin.json",
    "plugins/shared/mode_coding/mode.yaml",
    "kernel/crates/api/src/routes.rs",
    ".env",
    "config/rules/information_integrity_rules.md",
    # 裁决权载体（ADR 2026-09-16：被裁决者不可经提案改裁判）
    "config/plugins/evaluation/evaluation_metrics.yaml",
    "config/plugins/review/triage_rules.yaml",
    "plugins/shared/evaluation/plugin.py",
    "plugins/shared/review/improvement.py",
])
def test_validate_rejects_frozen_paths(target):
    violations = _validate(_valid_proposal(target=target), ROOT)
    assert any(v.startswith("frozen:") for v in violations), target


def test_validate_rejects_outside_whitelist_and_escape():
    assert any(v.startswith("whitelist:")
               for v in _validate(_valid_proposal(target="docs/x.md"), ROOT))
    assert any("逃逸" in v
               for v in _validate(_valid_proposal(target="../../etc/p"), ROOT))


def test_validate_rejects_base_hash_mismatch():
    violations = _validate(_valid_proposal(base_hash="deadbeef"), ROOT)
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
PANEL_ENDPOINT = "/ext/mode_evolution/page/evolution-panel"
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


# ── 面板数据快照（账本投影；有账本/空账本两态，口径与 eval_query health 域同源） ──
def test_panel_snapshot_groups_rounds_by_mode_with_baseline_delta():
    snap = aggregate.panel_snapshot(_PANEL_LEDGER)
    assert snap["rounds_total"] == 3
    coding = snap["modes"]["coding"]
    assert coding["rounds"] == 2
    assert coding["passed"] == 5
    assert coding["total"] == 8
    # 基线 = 该模式首轮通过数，各轮 delta 相对基线（scorecard 走势数据）
    assert [p["delta_vs_baseline"] for p in coding["series"]] == [0, 1]
    assert snap["modes"]["writing"]["series"][0]["failed_cases"] == ["w2"]


def test_panel_snapshot_projects_proposals_and_lever_health():
    snap = aggregate.panel_snapshot(_PANEL_LEDGER)
    assert snap["proposals_total"] == 2
    assert snap["proposals_applied"] == 1
    assert snap["proposals_rejected"] == 1
    assert snap["hit_rate"] == 0.5
    assert snap["reject_rate"] == 0.5
    assert snap["levers"]["签名簇 S1"] == {"applied": 1, "total": 1}
    assert {h["proposal_id"] for h in snap["hypotheses"]} == {"p1", "p2"}


def test_panel_snapshot_empty_ledger_all_placeholder_sources():
    snap = aggregate.panel_snapshot({})
    assert snap["modes"] == {}
    assert snap["rounds_total"] == 0
    assert snap["hypotheses"] == []
    assert snap["hit_rate"] is None
    assert snap["reject_rate"] is None
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
    assert snap["proposals_total"] == 2
    assert snap["proposals_applied"] == 1
    assert snap["hit_rate"] == 0.5
    assert snap["reject_rate"] == 0.0
    assert snap["levers"] == {}


def test_eval_query_health_shares_panel_aggregation(monkeypatch):
    """eval_query health 域与面板快照同一聚合口径（health_summary 同源复用）。"""
    server = _load_server_module()
    monkeypatch.setattr(server, "_load_ledger", lambda: _PANEL_LEDGER)
    out = asyncio.run(server.eval_query(domain="health", mode="coding"))
    assert out["success"] is True
    assert out["rounds"] == 3
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
    assert '"rounds_total": 0' in html
    assert '"hypotheses": []' in html
    assert SNAPSHOT_ANCHOR not in html


@pytest.mark.parametrize("kind", ["unknown_path", "wrong_method"])
def test_panel_unrouted_request_returns_404(kind):
    server = _load_server_module()
    if kind == "unknown_path":
        result = asyncio.run(server.http_handle(
            path="/ext/mode_evolution/page/does-not-exist", method="GET"))
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
    assert page["space"] == "workspace"
    assert page["slot"] == "tab"
    assert page["path"] == "/p/evolution_panel"
    # pluginId 与 manifest id 同名（G2 强制 /ext/{plugin_id}/** 命名空间，
    # 且 WebviewWidget 上行白名单按该前缀校验）
    assert page["props"] == {
        "pluginId": "mode_evolution",
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


# ── 日常线数据面（双线面板）：真实任务流量投影（pipeline-runs.list 行） ──
_DAILY_TODAY = "2026-10-15"


def _run(status, started, pipeline="pipe-1", thread="thread-a", run_id="r"):
    return {"run_id": run_id, "status": status, "started_at": started,
            "ended_at": "", "pipeline_id": pipeline, "thread_id": thread}


def test_daily_projection_counts_today_and_completion_rate():
    runs = [
        _run("completed", "2026-10-15T09:00:00+00:00"),
        _run("failed", "2026-10-15T10:00:00+00:00", pipeline="pipe-2"),
        _run("cancelled", "2026-10-15T11:00:00+00:00", pipeline="pipe-3"),
        _run("running", "2026-10-14T08:00:00+00:00"),
    ]
    d = aggregate.daily_projection(runs, today=_DAILY_TODAY)
    assert d["today"]["total"] == 3
    assert d["today"]["completed"] == 1
    assert d["today"]["failed"] == 2  # cancelled 计入失败面（用户中止=体验信号）
    assert d["today"]["completion_rate"] == round(1 / 3, 3)
    assert d["running"] == 1  # 在途为全窗计数，不冒充今日终态
    assert d["window_total"] == 4


def test_daily_projection_week_buckets_ascending_and_out_of_window():
    runs = [
        _run("completed", "2026-10-15T09:00:00+00:00"),
        _run("failed", "2026-10-09T09:00:00+00:00", pipeline="p-old"),
        _run("completed", "2026-10-20T09:00:00+00:00"),  # 窗口外（未来）：不入桶
    ]
    d = aggregate.daily_projection(runs, today=_DAILY_TODAY)
    week = d["week"]
    assert [w["date"] for w in week] == [
        "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12",
        "2026-10-13", "2026-10-14", "2026-10-15"]
    assert week[0]["total"] == 1
    assert week[0]["failed"] == 1
    assert week[1]["total"] == 0  # 空日照零值出，不是缺口
    assert week[-1] == {"date": "2026-10-15", "total": 1, "completed": 1, "failed": 0}


def test_daily_projection_recent_failures_cap_and_order():
    runs = [_run("failed", f"2026-10-15T0{i}:00:00+00:00", pipeline=f"p{i}")
            for i in range(12)]
    d = aggregate.daily_projection(runs, today=_DAILY_TODAY)
    assert len(d["recent_failures"]) == 8
    assert d["recent_failures"][0]["pipeline_id"] == "p0"
    assert d["recent_failures"][-1]["pipeline_id"] == "p7"


def test_daily_projection_none_gap_empty_and_malformed_rows():
    assert aggregate.daily_projection(None) is None  # 缺口透传，页面按缺口渲染
    empty = aggregate.daily_projection([], today=_DAILY_TODAY)
    assert empty["today"]["total"] == 0
    assert empty["today"]["completion_rate"] is None  # 分母 0 不猜测
    malformed = aggregate.daily_projection(
        [_run("completed", ""), _run("failed", "garbage"),
         _run("completed", "abcdefghijkl")], today=_DAILY_TODAY)
    assert malformed["window_total"] == 3
    assert malformed["today"]["total"] == 0
    assert len(malformed["recent_failures"]) == 1  # 失败行无日期也进信号流


def test_panel_snapshot_carries_daily_projection():
    daily = {"today": {"total": 3}}
    assert aggregate.panel_snapshot(_PANEL_LEDGER, daily=daily)["daily"] is daily
    assert aggregate.panel_snapshot(_PANEL_LEDGER)["daily"] is None


def test_panel_snapshot_hypotheses_carry_commit_for_changelog():
    """进化日志真连面：applied 提案条目带 commit（proposal(action="apply") 落账字段）。"""
    ledger = {"rounds": [], "proposals": {
        "p20261015_090000": {"outcome": "applied", "target": "t", "motivation": "m",
                             "commit": "abc1234"}}}
    snap = aggregate.panel_snapshot(ledger)
    assert snap["hypotheses"][0]["commit"] == "abc1234"


def _fake_runs(rows):
    """_load_recent_runs 替身工厂（同步工厂返回 async 函数，避免协程泄漏）。"""
    async def _inner(headers=None):
        return rows
    return _inner


def test_panel_page_embeds_real_daily_projection(monkeypatch):
    """供页时日常线接真：service-registry 行进快照（双线合流）。"""
    server = _load_server_module()
    monkeypatch.setattr(server, "_load_ledger", lambda: _PANEL_LEDGER)
    monkeypatch.setattr(server, "_load_recent_runs", _fake_runs([
        {"run_id": "r1", "status": "completed",
         "started_at": "2026-10-15T09:00:00+00:00",
         "pipeline_id": "pipe-1", "thread_id": "thread-a"}]))
    data, html = _decode_panel(asyncio.run(
        server.http_handle(path=PANEL_ENDPOINT, method="GET")))
    assert '"daily": {' in html
    assert '"completion_rate"' in html


def test_panel_page_daily_unavailable_serves_gap_snapshot(monkeypatch):
    """日常线拉取失败（能力未注入等）→ daily=null，页面按缺口态渲染不阻断供页。"""
    server = _load_server_module()
    monkeypatch.setattr(server, "_load_ledger", lambda: {})
    monkeypatch.setattr(server, "_load_recent_runs", _fake_runs(None))
    data, html = _decode_panel(asyncio.run(
        server.http_handle(path=PANEL_ENDPOINT, method="GET")))
    assert data["status"] == 200
    assert '"daily": null' in html


# ── 日常线供页通道：_load_recent_runs（service-registry 调用与缺口容错） ──
class _CapHandle:
    """capability 句柄替身：记录调用参数，返回注入的载荷。"""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        return self._payload


def _bind_service_registry(monkeypatch, payload):
    server = _load_server_module()
    handle = _CapHandle(payload)

    def fake_get_capability(name):
        if name == "service-registry":
            return handle
        raise KeyError(name)

    monkeypatch.setattr(server.plugin, "get_capability", fake_get_capability)
    return server, handle


def test_load_recent_runs_parses_rows_and_tenant_header(monkeypatch):
    """租户头透传 + data 信封拆包（与 _fetch_mode_profile 同款容忍）。"""
    server, handle = _bind_service_registry(
        monkeypatch, {"data": [{"run_id": "r1", "status": "completed"}]})
    rows = asyncio.run(server._load_recent_runs({"X-AgentOS-Tenant": "t1"}))
    assert rows == [{"run_id": "r1", "status": "completed"}]
    assert handle.calls == [("pipeline-runs.list", {"limit": 500, "tenant_id": "t1"})]


def test_load_recent_runs_envelope_and_tolerances(monkeypatch):
    """无租户头 → 缺省参数；裸数组信封直收；非数组载荷 → 缺口 None。"""
    server, handle = _bind_service_registry(monkeypatch, [])
    assert asyncio.run(server._load_recent_runs(None)) == []
    assert handle.calls == [("pipeline-runs.list", {"limit": 500})]
    server2, _ = _bind_service_registry(monkeypatch, {"nope": True})
    assert asyncio.run(server2._load_recent_runs(None)) is None


def test_load_recent_runs_capability_missing_returns_none(monkeypatch):
    """service-registry 未注入（KeyError）→ 缺口 None，不阻断供页。"""
    server = _load_server_module()

    def fake_get_capability(name):
        raise KeyError(name)

    monkeypatch.setattr(server.plugin, "get_capability", fake_get_capability)
    assert asyncio.run(server._load_recent_runs(None)) is None
