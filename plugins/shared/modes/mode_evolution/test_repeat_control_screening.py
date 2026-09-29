# @feature: FP-0.2.二 eval 统计功效补充（repeat/对照臂/轨迹筛选） | @ci: python-coverage
"""repeat 去噪 / 对照臂 / 轨迹库先行筛选 三补充的单测（ADR 2026-09-29）。

靶面：suite_reader repeat 展开、aggregate 全过口径归组、evolution_metrics
对照轮排除口径与 control_comparison、screening 证据核查、server round_type
落账 / eval.screen / 面板快照注入。

隔离：server 用例把 _EVAL_REPORTS/_PROJECT_ROOT 指向 tmp_path，真实读写 tmp。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))

import aggregate  # noqa: E402
import evolution_metrics  # noqa: E402
import screening  # noqa: E402
import suite_reader  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _load_server_module():
    """按唯一模块名装载 server.py（避开车道内裸名 server 的跨插件互覆）。"""
    path = os.path.join(os.path.dirname(__file__), "server.py")
    spec = importlib.util.spec_from_file_location("eval_harness_service_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


server = _load_server_module()


@pytest.fixture
def server_env(tmp_path, monkeypatch):
    """把账本目录与项目根都指到 tmp：真实落盘但不污染主仓。"""
    server = _load_server_module()
    reports = tmp_path / "reports" / "eval"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(server, "_EVAL_REPORTS", str(reports))
    monkeypatch.setattr(server, "_PROJECT_ROOT", str(project))
    monkeypatch.delenv(screening.LIBRARY_ENV, raising=False)
    return server


def _write_suite(tmp_path, cases):
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"name": "t", "mode": "coding", "cases": cases},
                                   allow_unicode=True), encoding="utf-8")
    return str(path)


# ── suite_reader：repeat 展开 ─────────────────────────────────────────────
def test_expand_repeat_emits_one_batch_per_run(tmp_path):
    """repeat=n → n 条批次：run_index 1..n、goal_title 带 -r 序号、预算随行。"""
    suite = _write_suite(tmp_path, [
        {"id": "c1", "messages": ["做 A"], "repeat": 2,
         "budget": {"max_rounds": 5}, "difficulty": "easy",
         "acceptance_criteria": {"file_check": {"input_params": {"path": "a.txt"}}}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), run_tag="T")
    assert [b["run_index"] for b in batches] == [1, 2]
    assert all(b["case_id"] == "c1" for b in batches)
    assert batches[0]["task_submit_args"]["goal_title"] == "eval-c1-T-r1"
    assert batches[1]["task_submit_args"]["goal_title"] == "eval-c1-T-r2"
    assert batches[1]["budget"] == {"max_rounds": 5}
    assert batches[1]["difficulty"] == "easy"


def test_expand_repeat_default_keeps_legacy_shape(tmp_path):
    """无 repeat → 单批次且 goal_title 不带序号（旧题集展开逐字不变）。"""
    suite = _write_suite(tmp_path, [
        {"id": "c1", "messages": ["做 A"], "acceptance_criteria": {}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), run_tag="T")
    assert len(batches) == 1
    assert batches[0]["run_index"] == 1
    assert batches[0]["task_submit_args"]["goal_title"] == "eval-c1-T"


@pytest.mark.parametrize("bad", [0, -1, "2", True, 1.5, []])
def test_expand_repeat_invalid_raises(tmp_path, bad):
    """repeat 非法（0/负数/字符串/bool/浮点/列表）→ ValueError（fail-closed）。"""
    suite = _write_suite(tmp_path, [
        {"id": "c1", "messages": ["x"], "repeat": bad, "acceptance_criteria": {}},
    ])
    with pytest.raises(ValueError, match="repeat"):
        suite_reader.expand_batches(suite_reader.load_suite(suite))


def test_expand_repeat_respects_case_filter(tmp_path):
    """cases 过滤命中 repeat 题 → 只展开该题的 n 条批次。"""
    suite = _write_suite(tmp_path, [
        {"id": "a", "messages": ["x"], "repeat": 2, "acceptance_criteria": {}},
        {"id": "b", "messages": ["y"], "acceptance_criteria": {}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite),
                                          cases="a", run_tag="T")
    assert [b["run_index"] for b in batches] == [1, 2]
    assert {b["case_id"] for b in batches} == {"a"}


# ── aggregate：全过口径归组 ───────────────────────────────────────────────
def test_summarize_repeat_partial_failure_fails_case():
    """2 次复跑 1 过 1 败 → case 不通过（全过口径）；逐次明细与失败分类齐全。"""
    results = [
        {"case_id": "c1", "task_id": "t1", "task_status": "completed",
         "criteria": {"file_check": True}},
        {"case_id": "c1", "task_id": "t2", "task_status": "failed",
         "criteria": {"file_check": False}},
    ]
    out = aggregate.summarize("coding", results)
    assert out["passed"] == 0
    assert out["total"] == 1
    assert out["repeat_detail"] == {"c1": [True, False]}
    row = out["results"][0]
    assert row["passed"] is False
    assert row["repeat_total"] == 2
    assert row["repeat_passed"] == 1
    assert row["repeat_pass_rate"] == 0.5
    assert row["task_status"] == "failed"
    assert row["behavior_class"] == ["task_failure"]


def test_summarize_repeat_all_pass_and_context_kept():
    """全次通过 → case 通过且 behavior_class 空；行上下文（criteria）保留。"""
    results = [
        {"case_id": "c1", "task_status": "completed",
         "criteria": {"file_check": True}, "budget": {"max_rounds": 5}},
        {"case_id": "c1", "task_status": "completed",
         "criteria": {"file_check": True}},
    ]
    out = aggregate.summarize("coding", results)
    assert out["passed"] == 1
    row = out["results"][0]
    assert row["passed"] is True
    assert row["behavior_class"] == []
    assert row["repeat_pass_rate"] == 1.0
    assert row["budget"] == {"max_rounds": 5}  # 首条目上下文保留


def test_summarize_without_repeats_is_legacy_shape():
    """无重复 case_id → 行无 repeat 键、汇总无 repeat_detail（旧口径逐字兼容）。"""
    results = [
        {"case_id": "c1", "task_status": "completed",
         "criteria": {"semantic_check": False}},
        {"case_id": "c2", "task_status": "failed", "criteria": {}},
    ]
    out = aggregate.summarize("coding", results)
    assert "repeat_detail" not in out
    by_id = {r["case_id"]: r for r in out["results"]}
    assert by_id["c1"]["behavior_class"] == ["acceptance_miss"]
    assert by_id["c2"]["behavior_class"] == ["task_failure"]
    for row in out["results"]:
        assert "repeat_total" not in row


# ── evolution_metrics：对照轮排除 + control_comparison ────────────────────
def _round(run_id, mode, passed, total, round_type=None, cases=None):
    r = {"run_id": run_id, "mode": mode, "passed": passed, "total": total}
    if round_type:
        r["round_type"] = round_type
    if cases is not None:
        r["case_results"] = cases
    return r


def test_control_rounds_excluded_from_learning_series():
    """对照轮混入不改变裁决斜率：verdict 的 P0 = 纯 treatment 序列斜率
    （learning_rates 本身是通用函数，verdict 才负责排除对照轮的契约）。"""
    treatment_only = [_round("t1", "coding", 5, 10),
                      _round("t2", "coding", 7, 10)]
    mixed = treatment_only + [_round("c1", "coding", 5, 10, round_type="control")]
    slope_pure = evolution_metrics.learning_rates(treatment_only)["coding"]["slope"]
    verdict = evolution_metrics.verdict({"rounds": mixed, "proposals": {}})
    assert verdict["p0"]["learning_rates"]["coding"]["slope"] == pytest.approx(slope_pure)
    # 区分度：若不排除，三点序列（含零变更对照）斜率会被压平
    slope_with_control = evolution_metrics.learning_rates(mixed)["coding"]["slope"]
    assert slope_with_control != pytest.approx(slope_pure)


def test_retention_and_regression_ignore_control_rounds():
    """保留率只看 treatment 相邻对（排除在 verdict 层）：中间的对照轮不构成
    转移、不误报退化。"""
    rounds = [
        _round("t1", "coding", 2, 2, cases={"a": True, "b": True}),
        # 对照轮只跑了 a（若不排除，t1→control 会被算成 b 退化）
        _round("c1", "coding", 1, 1, round_type="control", cases={"a": True}),
        _round("t2", "coding", 2, 2, cases={"a": True, "b": True}),
    ]
    verdict = evolution_metrics.verdict({"rounds": rounds, "proposals": {}})
    assert verdict["p1"]["retention"] == 1.0
    assert verdict["p1"]["regressed_cases"] == []


def test_control_comparison_pairs_latest_per_mode():
    """对照比较 = 每模式最近 treatment vs 最近 control；缺任一侧的模式不计入。"""
    rounds = [
        _round("t1", "coding", 7, 10),
        _round("c1", "coding", 5, 10, round_type="control"),
        _round("t2", "coding", 8, 10),
        _round("c2", "writing", 3, 10, round_type="control"),  # writing 无 treatment
    ]
    controls = evolution_metrics.control_comparison(rounds)
    assert set(controls) == {"coding"}
    assert controls["coding"] == {"treatment_rate": 0.8, "control_rate": 0.5,
                                  "delta": 0.3}
    verdict = evolution_metrics.verdict({"rounds": rounds, "proposals": {}})
    assert verdict["control"] == controls


def test_control_comparison_absent_or_zero_total():
    """无对照轮 → None；对照轮 total=0（无比率）→ 该模式不计入。"""
    assert evolution_metrics.control_comparison(
        [_round("t1", "coding", 5, 10)]) is None
    rounds = [_round("t1", "coding", 5, 10),
              _round("c1", "coding", 0, 0, round_type="control")]
    assert evolution_metrics.control_comparison(rounds) is None


def test_verdict_carries_control_key_and_version():
    """裁决输出携带 control 节与指标版本（p0p3-1.1 起含对照臂口径）。"""
    verdict = evolution_metrics.verdict({
        "rounds": [_round("t1", "coding", 5, 10),
                   _round("t2", "coding", 7, 10)],
        "proposals": {}})
    assert verdict["metrics_version"] == "p0p3-1.1"
    assert verdict["metrics_version"].startswith("p0p3-")
    assert "control" in verdict


# ── screening：轨迹库证据核查 ─────────────────────────────────────────────
def _record(pid, mode, outcome, title="", trajectory=True):
    return {"task_ref": pid, "mode": mode, "outcome": outcome, "title": title,
            "trajectory_available": trajectory}


def test_screen_sufficient_with_failure_cluster():
    """同模式 ≥2 条失败轨迹（题名命中）→ sufficient，可重放计数正确。"""
    library = {"records": [
        _record("p1", "coding", "failure", title="导出任务失败"),
        _record("p2", "coding", "failure", title="导出任务超时", trajectory=False),
        _record("p3", "coding", "success", title="导出任务完成"),
        _record("p4", "coding", "failure", title="无关题"),
        _record("p5", "research", "failure", title="导出任务失败"),
    ]}
    out = screening.screen({"mode": "coding", "title_substring": "导出"}, library)
    assert out["verdict"] == "sufficient"
    assert out["matched"] == 2
    assert out["failure_samples"] == 3  # coding 全部失败样本（含不匹配题名的 p4）
    assert out["replayable"] == 1
    assert out["threshold"] == screening.MIN_CLUSTER_EVIDENCE


def test_screen_insufficient_single_match():
    """仅 1 条命中 → insufficient（裁决非错误），reason 带阈值提示。"""
    library = {"records": [_record("p1", "coding", "failure", title="孤立失败")]}
    out = screening.screen({"mode": "coding"}, library)
    assert out["verdict"] == "insufficient"
    assert out["matched"] == 1
    assert "阈值" in out["reason"]


def test_screen_mode_isolation_and_empty_library():
    """跨模式失败不计入；空库/缺 records → insufficient 且提示先采集。"""
    library = {"records": [_record("p1", "research", "failure")]}
    out = screening.screen({"mode": "coding"}, library)
    assert out["verdict"] == "insufficient"
    assert out["failure_samples"] == 0

    empty = screening.screen({"mode": "coding"}, {})
    assert empty["verdict"] == "insufficient"
    assert "采集" in empty["reason"]


def test_library_summary_counts_and_modes():
    """库投影：总量/失败/可重放与按模式分桶。"""
    library = {"generated_tag": "20260929_120000", "records": [
        _record("p1", "coding", "failure"),
        _record("p2", "coding", "failure", trajectory=False),
        _record("p3", "coding", "success"),
        _record("p4", "writing", "failure"),
    ]}
    summary = screening.library_summary(library)
    assert summary["total"] == 4
    assert summary["failures"] == 3
    assert summary["replayable"] == 2
    assert summary["modes"]["coding"] == {"total": 3, "failures": 2, "replayable": 1}
    assert summary["modes"]["writing"] == {"total": 1, "failures": 1, "replayable": 1}


def test_find_latest_library_picks_lexicographic_latest(tmp_path):
    """tag=时间戳文件名字典序即时序：取最新；目录缺失返回空串。"""
    lib_dir = tmp_path / "trajectory_library"
    lib_dir.mkdir()
    (lib_dir / "20260101_000000.json").write_text("{}", encoding="utf-8")
    (lib_dir / "20260929_120000.json").write_text("{}", encoding="utf-8")
    latest = screening.find_latest_library(str(tmp_path))
    assert latest.endswith("20260929_120000.json")
    assert screening.find_latest_library(str(tmp_path / "nope")) == ""


# ── server：round_type 落账 / eval.screen / 面板注入 ──────────────────────
def test_summarize_round_type_control_recorded(server_env):
    """round_type=control 落账进轮行；缺省 treatment。"""
    out = asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}],
        round_type="control"))
    assert out["success"] is True
    rounds = server_env._load_ledger()["rounds"]
    assert rounds[-1]["round_type"] == "control"


def test_summarize_round_type_invalid_rejected(server_env):
    """非法 round_type → 错误值且不落账。"""
    out = asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}],
        round_type="placebo"))
    assert out["success"] is False
    assert "round_type 非法" in out["error"]
    assert server_env._load_ledger()["rounds"] == []


def test_summarize_baseline_skips_control_rounds(server_env):
    """基线链只认 treatment：夹在中间的对照轮不污染 treatment 间 delta。"""
    server_env._save_ledger({
        "rounds": [
            {"run_id": "t1", "mode": "coding", "passed": 2, "total": 3},
            {"run_id": "c1", "mode": "coding", "passed": 4, "total": 4,
             "round_type": "control"},
        ],
        "proposals": {}, "levers": {}})
    out = asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}},
        {"case_id": "c2", "task_status": "completed", "criteria": {"x": True}},
        {"case_id": "c3", "task_status": "completed", "criteria": {"x": True}},
    ]))
    assert out["delta"] == 1  # 3 - 2（treatment 基线），而非 3 - 4（对照轮）


def test_summarize_repeat_detail_lands_in_ledger(server_env):
    """同 case_id 逐次喂入 → 归组全过口径，轮行带 repeat_detail。"""
    out = asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}},
        {"case_id": "c1", "task_status": "failed", "criteria": {"x": False}},
    ]))
    assert out["passed"] == 0
    assert out["total"] == 1
    row = server_env._load_ledger()["rounds"][-1]
    assert row["repeat_detail"] == {"c1": [True, False]}
    assert row["case_results"] == {"c1": False}


def test_eval_screen_without_library_is_insufficient_not_error(server_env):
    """无轨迹库 → 裁决 insufficient（success=True），reason 指向采集通道。"""
    out = asyncio.run(server_env.eval_screen(mode="coding"))
    assert out["success"] is True
    assert out["verdict"] == "insufficient"
    assert "采集" in out["reason"]


def _write_library(reports_dir, tag="20260929_120000"):
    lib_dir = os.path.join(reports_dir, "trajectory_library")
    os.makedirs(lib_dir, exist_ok=True)
    path = os.path.join(lib_dir, f"{tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"generated_tag": tag, "records": [
            {"task_ref": "p1", "mode": "coding", "outcome": "failure",
             "title": "导出失败", "trajectory_available": True},
            {"task_ref": "p2", "mode": "coding", "outcome": "failure",
             "title": "导出超时", "trajectory_available": True},
        ]}, fh, ensure_ascii=False)
    return path


def test_eval_screen_picks_latest_library_and_sufficient(server_env):
    """字典序最新库文件被消费；命中 ≥2 → sufficient 且响应带库路径。"""
    _write_library(str(server_env._EVAL_REPORTS), tag="20260101_000000")
    path = _write_library(str(server_env._EVAL_REPORTS), tag="20260929_120000")
    out = asyncio.run(server_env.eval_screen(mode="coding", title_substring="导出"))
    assert out["success"] is True
    assert out["library"] == path
    assert out["verdict"] == "sufficient"
    assert out["matched"] == 2


def test_eval_screen_corrupt_library_is_error(server_env, monkeypatch):
    """env 指向损坏库文件 → 如实报错（不静默当空库）。"""
    path = os.path.join(str(server_env._EVAL_REPORTS), "broken.json")
    os.makedirs(server_env._EVAL_REPORTS, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ broken")
    monkeypatch.setenv(screening.LIBRARY_ENV, path)
    out = asyncio.run(server_env.eval_screen(mode="coding"))
    assert out["success"] is False
    assert "轨迹库读取失败" in out["error"]


def test_eval_screen_env_path_absent_file_is_insufficient(server_env, monkeypatch):
    """env 指向不存在文件 → 等价无库，裁决 insufficient（非错误）。"""
    monkeypatch.setenv(screening.LIBRARY_ENV,
                       os.path.join(str(server_env._EVAL_REPORTS), "nope.json"))
    out = asyncio.run(server_env.eval_screen(mode="coding"))
    assert out["success"] is True
    assert out["verdict"] == "insufficient"


def test_panel_library_summary_corrupt_library_renders_placeholder(server_env):
    """最新库文件损坏 → 面板投影返回 None（页面按占位渲染，不阻断供页）。"""
    os.makedirs(server_env._EVAL_REPORTS, exist_ok=True)
    lib_dir = os.path.join(str(server_env._EVAL_REPORTS), "trajectory_library")
    os.makedirs(lib_dir, exist_ok=True)
    with open(os.path.join(lib_dir, "20260929_120000.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{ broken")
    assert server_env._load_library_summary() is None


def test_panel_library_summary_absent_library_renders_placeholder(server_env):
    """无库目录 → 面板投影 None（占位渲染）。"""
    assert server_env._load_library_summary() is None


def test_panel_snapshot_carries_controls_round_type_and_library(server_env):
    """面板快照：对照比较/轮次类型/repeat 明细/库投影全部入快照。"""
    ledger = {
        "rounds": [
            {"run_id": "t1", "mode": "coding", "passed": 2, "total": 3,
             "failed_cases": ["c3"],
             "case_results": {"c1": True, "c2": True, "c3": False},
             "repeat_detail": {"c3": [True, False]}},
            {"run_id": "c1", "mode": "coding", "passed": 1, "total": 3,
             "round_type": "control"},
        ],
        "proposals": {}, "levers": {}}
    library = screening.library_summary(
        {"generated_tag": "g", "records": [_record("p1", "coding", "failure")]})
    snap = aggregate.panel_snapshot(ledger, library=library)
    assert snap["controls"]["coding"] == {"treatment_rate": 0.667,
                                          "control_rate": 0.333, "delta": 0.333}
    coding = snap["modes"]["coding"]
    assert coding["series"][0]["round_type"] == "treatment"
    assert coding["series"][0]["repeats"] == {"c3": [True, False]}
    assert coding["series"][1]["round_type"] == "control"
    assert snap["library"] == library


def test_panel_html_injects_controls_and_library(server_env):
    """供页注入链路：快照含 controls/library，新区块在 HTML 内有渲染挂点。"""
    _write_library(str(server_env._EVAL_REPORTS))
    server_env._save_ledger({
        "rounds": [_round("t1", "coding", 5, 10),
                   _round("c1", "coding", 4, 10, round_type="control")],
        "proposals": {}, "levers": {}})
    resp = server_env._panel_html_response()
    assert resp["status"] == 200
    html = base64.b64decode(resp["body"]).decode("utf-8")
    assert server._SNAPSHOT_ANCHOR not in html  # 锚点已被快照替换
    assert "对照臂" in html  # 新区块挂点存在
    assert "轨迹库样本" in html
    assert '"controls"' in html  # 快照数据已内联
    assert '"library"' in html


def _panel_html() -> str:
    with open(server._PANEL_HTML_PATH, encoding="utf-8") as fh:
        return fh.read()


def test_panel_html_contract_keeps_new_sections():
    """页面契约：新区块容器与渲染函数就位（防 HTML 回归剥离）。"""
    html = _panel_html()
    assert 'id="controls"' in html
    assert 'id="library"' in html
    assert "function renderControls" in html
    assert "function renderLibrary" in html
    assert "bar-fill.control" in html


def test_panel_modes_render_is_data_driven():
    """模式分组数据驱动：账本里的未知模式也出卡片（前端连接账本而非硬编码）。"""
    html = _panel_html()
    assert "keys.indexOf(k) === -1" in html
    assert "modeLabel" in html
