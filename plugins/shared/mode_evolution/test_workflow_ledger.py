# @feature: FP-0.2.批C-a-3 回顾工作流图（步骤节点落账） @vision: V1 可进化 @ci: plugins-coverage
"""回顾工作流事件层 + 投影 + 服务端埋点 的行为测试（不依赖内核）。

断言口径（全部断可观察行为，不碰内部实现）：
- 事件层：seq 严格递增、未知步骤拒写、end 幂等、缺工作流不造幽灵、原子写可回读；
- 投影层：节点数 == 事件数、投影幂等、traces 派生不重复造同一步节点、改进产物挂节点；
- 服务端：四个工具 handler 调用后真落节点（含 round_id 软关联、reject category→步骤）。

防拟合：每类行为至少两组有区分度输入（正常轮 / 空账 / 回边轮 / 对照轮），
关键行为配性质断言，落盘走真实文件系统（tmp_path），不 mock open。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import aggregate  # noqa: E402
import workflow_ledger as wf  # noqa: E402

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL = os.path.join(_PLUGIN_DIR, "webview", "evolution_panel.html")


@pytest.fixture(autouse=True)
def _isolated_wf_dir(tmp_path, monkeypatch):
    """工作流目录指到 tmp（真实落盘，不污染主仓）。"""
    monkeypatch.setenv(wf.ENV_WF_DIR, str(tmp_path / "wf"))
    return tmp_path / "wf"


def _load_server(alias: str):
    """按唯一模块名装载 server.py（避开车道内裸名 server 的跨插件互覆）。"""
    path = os.path.join(_PLUGIN_DIR, "server.py")
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(tmp_path, monkeypatch):
    """server 模块 + 账本目录指到 tmp（工作流目录由 autouse fixture 负责）。"""
    module = _load_server("mode_evolution_workflow_server")
    reports = tmp_path / "reports" / "eval"
    monkeypatch.setattr(module, "_EVAL_REPORTS", str(reports))
    monkeypatch.setattr(module, "_git_head", lambda: "")
    return module


def _decode(result: dict) -> str:
    return base64.b64decode(result["data"]["body"]).decode("utf-8")


def _snapshot_of(html: str) -> dict:
    marker = "window.__EVOLUTION_SNAPSHOT__ = "
    start = html.find(marker)
    assert start > 0, "面板页缺快照赋值"
    end = html.find("\n", start)
    raw = html[start + len(marker):end].rstrip(";")
    return json.loads(raw)


# ── 步骤枚举声明（flow_steps.yaml 被真实消费）────────────────────────────

def test_step_catalog_reads_declared_flow_steps():
    """步骤枚举来自 rules/flow_steps.yaml（不是代码里写死的列表）。"""
    steps = wf.step_catalog()
    assert [d["step"] for d in steps][:3] == ["T1", "T2", "T3"]
    by_step = {d["step"]: d for d in steps}
    assert by_step["T1"]["title"] == "测量"
    assert by_step["T1"]["downstream"] == ["T2"]
    assert by_step["T3"]["is_gate"] is True
    assert by_step["T6.5"]["is_gate"] is True
    # 前向后继与上游必须互指（回边不在此列，见 edges 的 kind=back）
    back = {(e["from"], e["to"]) for e in wf.flow_edges() if e["kind"] == "back"}
    for d in steps:
        for down in d["downstream"]:
            assert (d["step"], down) not in back, f"{d['step']}->{down} 回边误声明成前向"
            assert d["step"] in by_step[down]["upstream"], f"{d['step']}->{down} 上游未互指"


def test_flow_edges_declared_as_back_edges():
    """回边由声明驱动（面板据此画虚线），且都能在步骤表里找到端点。"""
    edges = wf.flow_edges()
    assert {(e["from"], e["to"]) for e in edges if e["kind"] == "back"} >= {
        ("T5", "T3"), ("T8", "T1")}
    ids = set(wf.step_ids())
    for e in edges:
        assert e["from"] in ids
        assert e["to"] in ids


@pytest.mark.parametrize(("category", "expected"), [
    ("prediction_miss", "T4"),
    ("heldout_gap", "T5"),
    ("sentry_regression", "T6.5"),
    ("user_veto", "T6"),
    ("external_cause", "T2"),
    ("不存在的类别", "T2"),
])
def test_reject_step_maps_category_to_step(category, expected):
    """拒绝类别 → 归属步骤（裁决发生在哪步就落哪步）；未知类别回落归因 T2。"""
    assert wf.reject_step(category) == expected


# ── 事件层不变式 ────────────────────────────────────────────────────

def test_begin_end_roundtrip_keeps_seq_strictly_increasing():
    """两次 begin 的 seq 严格递增，end 后状态/产出可回读（真实落盘再读）。"""
    first = wf.begin_step("w1", "T1", inputs={"tier": "smoke"}, driven_by="tool:eval_query")
    second = wf.begin_step("w1", "T3", inputs={}, driven_by="tool:proposal")
    assert first["seq"] == 1
    assert second["seq"] == 2
    wf.end_step("w1", first["seq"], artifacts={"passed": 3, "total": 4})
    doc = wf.load_workflow("w1")
    assert doc is not None
    nodes = doc["steps"]
    assert [n["seq"] for n in nodes] == [1, 2]
    assert nodes[0]["status"] == "done"
    assert nodes[0]["artifacts"]["passed"] == 3
    assert nodes[1]["status"] == "running"
    assert nodes[1]["ended_at"] == ""


@pytest.mark.parametrize("bad_step", ["T0", "t1", "", "T9", "测量"])
def test_unknown_step_is_rejected_not_silently_written(bad_step):
    """不在声明表内的步骤名拒写（返回空 + 不落野值）。"""
    assert wf.begin_step("w2", bad_step) == {}
    assert wf.load_workflow("w2") is None
    assert wf.finish_step("w2", bad_step) == {}


def test_end_step_is_idempotent_and_never_overwrites_first_result():
    """重复 end 返回首值：状态/结束时间/产出不被第二次调用改写。"""
    begun = wf.begin_step("w3", "T1")
    first = wf.end_step("w3", begun["seq"], artifacts={"passed": 2, "total": 2})
    second = wf.end_step("w3", begun["seq"], status="failed", artifacts={"passed": 0})
    assert second.get("idempotent") is True
    assert second["status"] == first["status"] == "done"
    node = wf.load_workflow("w3")["steps"][0]
    assert node["artifacts"]["passed"] == 2


def test_end_step_on_missing_workflow_creates_no_ghost():
    """缺工作流时 end_step 不造幽灵文件（无证据不造假）。"""
    assert wf.end_step("no-such-wf", 1) == {}
    assert wf.load_workflow("no-such-wf") is None


def test_finish_step_appends_node_when_no_running_one():
    """无 running 节点时 finish 补一条已完成节点（步骤确实发生过 = 事件即证据）。"""
    out = wf.finish_step("w4", "T6", artifacts={"commit": "abc1234"})
    assert out["status"] == "done"
    node = wf.load_workflow("w4")["steps"][0]
    assert node["step"] == "T6"
    assert node["ended_at"] != ""
    assert node["artifacts"]["commit"] == "abc1234"


def test_close_workflow_sets_status_and_reason_and_is_idempotent():
    """收口写 status/close_reason/closed_at；重复收口幂等；非法出口落 unknown。"""
    wf.begin_step("w5", "T1")
    closed = wf.close_workflow("w5", "vetoed")
    assert closed["status"] == "closed"
    assert closed["close_reason"] == "vetoed"
    again = wf.close_workflow("w5", "budget_exhausted")
    assert again.get("idempotent") is True
    assert again["close_reason"] == "vetoed"
    doc = wf.load_workflow("w5")
    assert doc["status"] == "closed"
    assert doc["closed_at"] != ""
    # 非法出口不编造：落 unknown
    wf.begin_step("w6", "T1")
    assert wf.close_workflow("w6", "我瞎编的出口")["close_reason"] == "unknown"


def test_resolve_wf_id_rotates_after_close():
    """关闭后同一基准自动轮转到新实例（不复活已收口的工作流）。"""
    wf.begin_step("base", "T1")
    wf.close_workflow("base", "vetoed")
    rotated = wf.resolve_wf_id("")
    assert rotated not in ("", "base")
    # 显式 id 原样透传
    assert wf.resolve_wf_id("base") == "base"


def test_list_workflows_orders_by_opened_at_and_skips_pointer():
    """列表按 opened_at 升序、跳过活动指针文件、超限保留最新。"""
    for wid in ("a1", "a2", "a3"):
        wf.begin_step(wid, "T1", mode="coding")
    out = wf.list_workflows(limit=2)
    assert [d["wf_id"] for d in out] == ["a2", "a3"]
    names = [p.name for p in Path(wf.wf_dir()).iterdir()]
    assert "_current.json" in names            # 活动指针确实存在
    assert all(d["wf_id"] not in names for d in out)  # 但不会被当成工作流返回


# ── 投影层 ─────────────────────────────────────────────────────────

def _workflow_doc(wf_id: str, steps: list[tuple[str, str]],
                  status: str = "running") -> dict:
    """按 (step, status) 序列造一个工作流文档（真实结构，不走写入面）。"""
    return {
        "wf_id": wf_id, "mode": "coding", "round_type": "treatment",
        "status": status, "close_reason": "" if status == "running" else "vetoed",
        "opened_at": f"2026-01-01T00:00:0{len(wf_id) % 10}",
        "closed_at": "" if status == "running" else "2026-01-01T00:10:00",
        "pipeline_id": "pipe-1",
        "steps": [{"seq": i + 1, "step": s, "status": st,
                   "started_at": "2026-01-01T00:00:01",
                   "ended_at": "" if st == "running" else "2026-01-01T00:00:02",
                   "inputs": {}, "artifacts": {}, "driven_by": "tool:x"}
                  for i, (s, st) in enumerate(steps)],
    }


def test_projection_keeps_one_node_per_event_including_back_edge():
    """节点数 == 事件数；回边产生的第二个 T3 是独立节点而非覆盖。"""
    doc = _workflow_doc("wf1", [("T1", "done"), ("T3", "done"), ("T4", "done"),
                                ("T5", "done"), ("T3", "done")])
    proj = aggregate.workflow_projection({}, [doc], defs=wf.step_catalog(),
                                         edges=wf.flow_edges())
    nodes = proj["instances"][0]["nodes"]
    assert len(nodes) == 5
    assert [n["step"] for n in nodes].count("T3") == 2
    assert [n["seq"] for n in nodes] == [1, 2, 3, 4, 5]
    assert proj["focus"] == "wf1"
    assert proj["summary"]["nodes_total"] == 5


def test_projection_is_idempotent():
    """同输入二次投影结果完全相同（纯函数性质）。"""
    doc = _workflow_doc("wf2", [("T1", "done"), ("T6.5", "skipped")])
    ledger = {"proposals": {"p1": {"outcome": "applied"}}, "rounds": []}
    one = aggregate.workflow_projection(ledger, [doc], defs=wf.step_catalog(),
                                        edges=wf.flow_edges())
    two = aggregate.workflow_projection(ledger, [doc], defs=wf.step_catalog(),
                                        edges=wf.flow_edges())
    assert one == two


def test_projection_empty_ledger_reports_gap_and_no_instances():
    """空账 → instances=[] 且 gaps 记录空态原因（页面按骨架 + 空态渲染）。"""
    proj = aggregate.workflow_projection({}, [], defs=wf.step_catalog(),
                                         edges=wf.flow_edges(), trace_steps=None)
    assert proj["instances"] == []
    assert proj["focus"] == ""
    keys = {g["reason"] for g in proj["gaps"]}
    assert "no_workflow_data" in keys
    assert "traces_unavailable" in keys
    assert [d["step"] for d in proj["defs"]][0] == "T1"


def test_projection_links_proposals_to_nodes_and_marks_unlinked():
    """改进产物挂到节点（touches 按 seq）；无触点的历史提案落未关联。"""
    doc = _workflow_doc("wf3", [("T3", "done"), ("T6", "done")])
    doc["steps"][0]["artifacts"] = {"proposal_ids": ["p1"]}
    doc["steps"][1]["artifacts"] = {"proposal_ids": ["p1"], "commit": "abc1234"}
    ledger = {"proposals": {"p1": {"outcome": "applied", "round_id": "r1"},
                            "p9": {"outcome": "applied"}}}
    proj = aggregate.workflow_projection(ledger, [doc], defs=wf.step_catalog(),
                                         edges=wf.flow_edges())
    threads = {t["proposal_id"]: t for t in proj["threads"]}
    assert [x["step"] for x in threads["p1"]["touches"]] == ["T3", "T6"]
    assert threads["p1"]["linked"] is True
    assert threads["p1"]["outcome"] == "applied"
    assert threads["p9"]["linked"] is False
    assert threads["p9"]["touches"] == []
    assert proj["summary"]["proposals_linked"] == 1
    assert proj["summary"]["proposals_unlinked"] == 1


def test_projection_does_not_duplicate_trace_derived_step():
    """traces 派生的步骤只在工具事件缺席时补节点（同一事实只留一份）。"""
    doc = _workflow_doc("wf4", [("T1", "done")])
    proj = aggregate.workflow_projection(
        {}, [doc], defs=wf.step_catalog(), edges=wf.flow_edges(),
        trace_steps={"wf4": [{"step": "T2", "at": "2026-01-01T00:00:03",
                              "evidence": "tool:review_improvement"},
                             {"step": "T1", "at": "2026-01-01T00:00:04",
                              "evidence": "tool:x"}]})
    nodes = proj["instances"][0]["nodes"]
    assert [n["step"] for n in nodes] == ["T1", "T2"]
    t2 = [n for n in nodes if n["step"] == "T2"][0]
    assert t2["driven_by"] == "traces.list_by_pipeline"
    assert t2["artifacts"]["evidence"] == "tool:review_improvement"


@pytest.mark.parametrize(("rows", "expected"), [
    ([{"patch_data": {"execution_contexts": [{"tool_name": "review_improvement"}]}}], ["T2"]),
    ([{"patch_data": {"execution_contexts": [{"tool_name": "memory"}]}}], ["T7"]),
    ([{"patch_data": {"execution_contexts": [{"tool_name": "bash"}]}}], []),
    ([{"patch_data": {"tool_name": "memory"}}], ["T7"]),
    ([{"patch_data": None}, "not-a-dict"], []),
])
def test_trace_rows_derive_t2_t7_by_tool_evidence(rows, expected):
    """traces 行 → 派生步骤：工具名是证据，无证据不出节点。"""
    server = _load_server("mode_evolution_workflow_server_trace")
    assert [s["step"] for s in server._trace_rows_to_steps(rows)] == expected


# ── 服务端埋点（工具 handler → 节点） ─────────────────────────────────

def test_eval_summarize_writes_t1_and_t65_nodes(tmp_path, server):
    """一轮聚合后：T1 收口（带 run_id/通过数）+ T6.5 保留率复验节点。"""
    asyncio.run(server._wf_begin("T1", "eval_query", mode="coding",
                                 inputs={"suite": "s", "tier": "smoke"}))
    out = asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}},
        {"case_id": "c2", "task_status": "completed", "criteria": {"x": False}},
    ], model="m1"))
    assert out["success"] is True
    docs = wf.list_workflows(limit=10)
    assert len(docs) == 1
    nodes = {n["step"]: n for n in docs[0]["steps"]}
    assert nodes["T1"]["status"] == "done"
    assert nodes["T1"]["artifacts"]["run_id"] == out["run_id"]
    assert nodes["T1"]["artifacts"]["passed"] == 1
    assert nodes["T1"]["artifacts"]["total"] == 2
    # 首轮无前序 treatment 轮 → T6.5 显式 skipped，不冒领"零退化"
    assert nodes["T6.5"]["status"] == "skipped"
    assert nodes["T6.5"]["artifacts"]["reason"] == "no_previous_round"


def test_second_round_detects_real_regression(tmp_path, server):
    """第二轮同 case 由过转败 → T6.5 落退化名单（真实观测，非编造）。"""
    asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}]))
    asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": False}}]))
    t65 = [n for n in wf.list_workflows(limit=10)[0]["steps"] if n["step"] == "T6.5"]
    assert [n["status"] for n in t65] == ["skipped", "done"]
    assert t65[1]["artifacts"]["regressed_cases"] == ["c1"]


def test_control_round_marks_t65_skipped(tmp_path, server):
    """对照轮不入保留率口径 → T6.5 记 skipped/control_round（不参与退化判定）。"""
    asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}]))
    asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": False}}],
        round_type="control"))
    t65 = [n for n in wf.list_workflows(limit=10)[0]["steps"] if n["step"] == "T6.5"]
    assert t65[-1]["status"] == "skipped"
    assert t65[-1]["artifacts"]["reason"] == "control_round"


def test_proposal_submit_writes_t3_and_round_id(tmp_path, server):
    """提案提交：条目回填 round_id（当轮 run_id）+ 落 T3 节点带 proposal_ids。"""
    out = asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}]))
    sub = asyncio.run(server._proposal_submit(
        layer="L1", target="config/agents/main/task_dispatch_guide.md",
        change_type="content_edit", content="x", motivation="签名簇 S1"))
    assert sub["accepted"] is True
    assert sub["round_id"] == out["run_id"]
    entry = server._load_ledger()["proposals"][sub["proposal_id"]]
    assert entry["round_id"] == out["run_id"]
    t3 = [n for n in wf.list_workflows(limit=10)[0]["steps"] if n["step"] == "T3"][-1]
    assert t3["artifacts"]["proposal_ids"] == [sub["proposal_id"]]
    assert t3["artifacts"]["accepted"] is True


def test_proposal_submit_without_round_context_keeps_round_id_empty(tmp_path, server):
    """无轮次上下文（未跑过 summarize）→ round_id 空串，不写假 ID、不报错。"""
    sub = asyncio.run(server._proposal_submit(
        layer="L1", target="config/agents/main/task_dispatch_guide.md",
        change_type="content_edit", content="x", motivation="签名簇 S1"))
    assert sub["success"] is True
    assert sub["round_id"] == ""
    assert server._load_ledger()["proposals"][sub["proposal_id"]]["round_id"] == ""


def test_proposal_reject_lands_node_on_category_step(tmp_path, server):
    """reject 的 category 决定落哪一步的节点（T4/T5/T6.5/T6 各自归位）。"""
    first = asyncio.run(server._proposal_submit(
        layer="L1", target="config/agents/main/task_dispatch_guide.md",
        change_type="content_edit", content="a", motivation="签名簇 S1"))
    asyncio.run(server._proposal_reject(first["proposal_id"], "预测失准", "prediction_miss"))
    second = asyncio.run(server._proposal_submit(
        layer="L1", target="config/agents/main/task_dispatch_guide.md",
        change_type="content_edit", content="b", motivation="签名簇 S1"))
    asyncio.run(server._proposal_reject(second["proposal_id"], "隐藏集不过", "heldout_gap"))
    steps = [n["step"] for n in wf.list_workflows(limit=10)[0]["steps"]]
    assert "T4" in steps
    assert "T5" in steps


def test_eval_health_closes_workflow_only_after_nodes_exist(tmp_path, server):
    """前置 health 体检不落 T8；有节点后的 health 落 T8 并收口工作流。"""
    asyncio.run(server._eval_health())
    assert wf.list_workflows(limit=10) == []
    asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}]))
    asyncio.run(server._eval_health())
    doc = wf.list_workflows(limit=10)[0]
    assert doc["status"] == "closed"
    assert [n["step"] for n in doc["steps"]][-1] == "T8"


# ── 面板（同图 + 空态 + 自包含 + 语法门禁） ───────────────────────────

def test_panel_carries_workflow_channel_and_no_hardcoded_step_coordinates(tmp_path, server):
    """面板快照带 workflow 通道；面板源码里不再有八步坐标数组（布点数据驱动）。"""
    asyncio.run(server.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}}]))
    html = _decode(asyncio.run(server.http_handle(
        path=server._PANEL_ENDPOINT, method="GET")))
    snap = _snapshot_of(html)
    assert snap["workflow"]["defs"], "快照缺 workflow.defs（骨架来源）"
    assert snap["workflow"]["edges"], "快照缺 workflow.edges（回边来源）"
    assert snap["workflow"]["instances"], "快照缺本次实例节点"
    with open(_PANEL, encoding="utf-8") as fh:
        page = fh.read()
    # 旧的写死八步坐标数组（['T1','测量',70,36] 形态）必须已消失
    assert "'测量', 70, 36" not in page
    assert "70, 36" not in page
    assert "460, 170" not in page
    assert "function renderRing" not in page
    # 新渲染器由图数据布点
    assert "function renderWorkflow" in page
    assert "function wfLayout" in page


def test_panel_renders_skeleton_plus_empty_state_without_status_nodes(tmp_path, server):
    """无历史数据：骨架在 + 空态文案在；不出现状态色节点（无伪游标）。"""
    html = _decode(asyncio.run(server.http_handle(
        path=server._PANEL_ENDPOINT, method="GET")))
    snap = _snapshot_of(html)
    assert snap["workflow"]["instances"] == []
    assert snap["workflow"]["gaps"]
    assert "尚无工作流记录" in html  # 空态文案
    assert "status-colored" not in html


def test_panel_keeps_information_architecture_markers_and_single_script(tmp_path, server):
    """信息架构 marker 全在（含 T1 测量 / T8 循环决策）、内联脚本仍单块。"""
    html = _decode(asyncio.run(server.http_handle(
        path=server._PANEL_ENDPOINT, method="GET")))
    for marker in ("轮次时间线", "scorecard 走势", "假设清单", "杠杆命中率",
                   "健康度五指标", "熔断状态", "T1 测量", "T8 循环决策",
                   "自进化指标", "P0-P3", "回顾工作流图"):
        assert marker in html, f"缺信息架构区块 {marker}"
    assert html.lower().count("<script") == 1


def test_panel_inline_script_passes_node_syntax_gate():
    """内联脚本语法门禁（该文件曾因引号错误整块失效，红了页面全空）。"""
    proc = subprocess.run(
        [sys.executable,
         os.path.join(_PLUGIN_DIR, "..", "..", "..",
                      "scripts", "check_webview_inline_script_syntax.py"), _PANEL],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
