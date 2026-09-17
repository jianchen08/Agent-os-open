# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
# -*- coding: utf-8 -*-
"""mode_coding 活面板行为测试：六列落列状态机 / reviews 代理 / dispatch_issue 派发。

对标 Devin/Cursor/Cline 的编码面板（ADR 2026-09-17-mode-panel-mature-interfaces）：
行为断言走 fake provider 捕获 args 形状，禁止真派发任务；provider 缺席路径
（单测/内核握手前）断言诚实降级空载荷。manifest/页面契约在 test_mode_panel_pages.py。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.unit


def _load_server():
    """按唯一模块名装载 mode_coding server.py（与 pages 契约测试隔离 provider 态）。"""
    path = os.path.join(MODES_DIR, "mode_coding", "server.py")
    spec = importlib.util.spec_from_file_location("mode_live_mode_coding", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call(module, path: str, method: str = "GET", raw_body: str = "", query: dict | None = None) -> dict:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query or {})
    )


def _body_json(result: dict) -> dict:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _envelope_status(result: dict) -> int:
    """边界状态断言走 envelope data.status（HttpHandleResponse 契约：HTTP 语义
    落信封，body 只带 {"error": 消息}）。"""
    return int(result["data"]["status"])


def _row(pid: str, **over: Any) -> dict:
    base = {
        "pipeline_id": pid, "thread_id": f"th-{pid}", "agent_id": "main",
        "run_status": "running", "mode": "coding", "task.goal": f"修复 {pid}",
        "task.status": "running", "task.id": f"task-{pid}",
        "current_phase": "plan", "ckpt_max_seq": 3, "message_count": 6, "input": "",
    }
    base.update(over)
    return base


def _set_state(module, rows: list[dict]) -> None:
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=rows))


def _column_of(board: dict, key: str) -> list[dict]:
    return next(c for c in board["columns"] if c["key"] == key)["cards"]


def _pids(cards: list[dict]) -> set[str]:
    return {c["pipeline_id"] for c in cards}


_EP = "/ext/mode_coding/data"


# ── 修复流水线：六列落列状态机（诚实启发式的口径契约）────────────────────────────

def test_board_lands_rows_by_pipeline_state_machine() -> None:
    """≥4 组有区分度输入：pending→issue / phase 含 worktree→worktree 列 /
    phase 含 test→测试 / pending_evaluation→审批 / completed→合并 /
    failed→issue（重开）/ 其余在途（running）→分诊。"""
    module = _load_server()
    rows = [
        _row("p-pending", **{"task.status": "pending"}),
        _row("p-worktree", **{"current_phase": "worktree_setup"}),
        _row("p-test", **{"current_phase": "run_tests"}),
        _row("p-review", **{"task.status": "pending_evaluation"}),
        _row("p-merged", **{"task.status": "completed", "run_status": "completed"}),
        _row("p-failed", **{"run_status": "failed"}),
        _row("p-triage"),  # run_status=running 在途且无更具体信号
    ]
    _set_state(module, rows)
    board = _body_json(_call(module, f"{_EP}/board"))
    assert "p-pending" in _pids(_column_of(board, "issue"))
    assert "p-worktree" in _pids(_column_of(board, "worktree"))
    assert "p-test" in _pids(_column_of(board, "testing"))
    assert "p-review" in _pids(_column_of(board, "review"))
    assert "p-merged" in _pids(_column_of(board, "merged"))
    # failed 重开回 issue 列；running 在途落分诊
    assert "p-failed" in _pids(_column_of(board, "issue"))
    assert "p-triage" in _pids(_column_of(board, "triage"))
    # 每张卡都落在恰好一列（不重不漏）
    all_cards = [c for col in board["columns"] for c in col["cards"]]
    assert len(all_cards) == len(rows) == len({c["pipeline_id"] for c in all_cards})


def test_board_columns_canonical_order_and_labels() -> None:
    """六列列序固定（issue→分诊→worktree→测试→审批→合并），空数据不缺列。"""
    module = _load_server()
    board = _body_json(_call(module, f"{_EP}/board"))
    assert [(c["key"], c["label"]) for c in board["columns"]] == [
        ("issue", "issue"), ("triage", "分诊"), ("worktree", "worktree"),
        ("testing", "测试"), ("review", "审批"), ("merged", "合并"),
    ]
    assert all(col["cards"] == [] for col in board["columns"])


def test_board_checkpoints_follow_ckpt_watermark() -> None:
    """checkpoint 数 = ckpt_max_seq 水位；内核无消息水位 -1 钳到 0（计数口径）。"""
    module = _load_server()
    _set_state(module, [
        _row("p-ckpt", **{"ckpt_max_seq": 5, "message_count": 11}),
        _row("p-empty", **{"ckpt_max_seq": -1, "message_count": 0}),
    ])
    board = _body_json(_call(module, f"{_EP}/board"))
    cards = {c["pipeline_id"]: c for col in board["columns"] for c in col["cards"]}
    assert cards["p-ckpt"]["checkpoints"] == 5
    assert cards["p-ckpt"]["message_count"] == 11
    assert cards["p-empty"]["checkpoints"] == 0


def test_board_precedence_follows_spec_signal_order() -> None:
    """信号同现时按规格枚举序取先命中（落列口径的优先级契约）：
    current_phase 相位信号优先于 task 终态——pending_evaluation+worktree 相位
    → worktree 列；completed+test 相位 → 测试列。"""
    module = _load_server()
    _set_state(module, [
        _row("p-review-worktree", **{"task.status": "pending_evaluation",
                                     "current_phase": "worktree_setup"}),
        _row("p-done-tested", **{"task.status": "completed", "run_status": "completed",
                                 "current_phase": "run_tests"}),
    ])
    board = _body_json(_call(module, f"{_EP}/board"))
    assert "p-review-worktree" in _pids(_column_of(board, "worktree"))
    assert "p-done-tested" in _pids(_column_of(board, "testing"))


# ── 会话列表 / 对话流（mode 键过滤 + 归一 + 缺参边界）────────────────────────────

def test_sessions_filter_mode_and_normalize() -> None:
    module = _load_server()
    _set_state(module, [
        _row("p-coding", **{"task.goal": "修复登录超时"}),
        _row("p-other", **{"mode": "roleplay"}),  # 他模式行必须被过滤
    ])
    sessions = _body_json(_call(module, f"{_EP}/sessions"))["sessions"]
    assert [s["pipeline_id"] for s in sessions] == ["p-coding"]
    row = sessions[0]
    assert row["goal"] == "修复登录超时"
    assert row["task_status"] == "running" and row["run_status"] == "running"
    assert row["task_id"] == "task-p-coding" and row["current_phase"] == "plan"
    assert isinstance(row["column"], str)


def test_sessions_goal_falls_back_to_input() -> None:
    """task.goal 缺席时诚实回退 input 摘要（内核早期行尚未写 task.*）。"""
    module = _load_server()
    _set_state(module, [_row("p-early", **{"task.goal": "", "input": "修一下 flaky 测试"})])
    sessions = _body_json(_call(module, f"{_EP}/sessions"))["sessions"]
    assert sessions[0]["goal"] == "修一下 flaky 测试"


def test_messages_query_contract() -> None:
    module = _load_server()
    # 缺 pipeline_id → 400；错误 method → 404
    assert _envelope_status(_call(module, f"{_EP}/messages")) == 400
    assert _envelope_status(_call(module, f"{_EP}/messages", method="POST")) == 404
    # 正常查询：归一 + provider 传参
    captured: dict = {}

    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        captured["pipeline_id"] = pipeline_id
        return [
            {"role": "user", "content_preview": "修一下", "status": "success", "created_at": "t1"},
            {"role": "assistant", "content_preview": "已定位根因", "status": "success", "created_at": "t2"},
        ]

    module._set_provider("messages", _provider)
    body = _body_json(_call(module, f"{_EP}/messages", query={"pipeline_id": "p-coding"}))
    assert captured["pipeline_id"] == "p-coding"
    assert body["messages"][0] == {
        "role": "user", "content": "修一下", "status": "success", "created_at": "t1",
    }
    assert body["messages"][1]["role"] == "assistant"


# ── diff 复盘：待审批列表（服务端代理 interaction.get_pending）────────────────────

def test_reviews_proxied_via_tool_executor() -> None:
    """reviews = tool-executor invoke interaction.get_pending（human_interaction_tool）；
    请求行归一 [{request_id,title,status,created_at}]，兼容嵌套/扁平两种请求行形状。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {
            "data": {
                "requests": [
                    {  # 真实服务行形状：id + title 藏在 message_data
                        "id": "req-1", "status": "pending", "created_at": "2026-09-17T10:00:00+00:00",
                        "message_data": {"title": "审批：修复登录超时的 diff"},
                    },
                    {  # 扁平形状兜底
                        "request_id": "req-2", "title": "选择修复方案", "status": "pending",
                        "created_at": "2026-09-17T10:05:00+00:00",
                    },
                ],
                "count": 2,
            }
        }

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(_call(module, f"{_EP}/reviews"))
    assert captured["tool_name"] == "interaction.get_pending"
    assert captured["plugin_id"] == "human_interaction_tool"
    assert captured["args"] == {}
    assert body["items"] == [
        {"request_id": "req-1", "title": "审批：修复登录超时的 diff",
         "status": "pending", "created_at": "2026-09-17T10:00:00+00:00"},
        {"request_id": "req-2", "title": "选择修复方案",
         "status": "pending", "created_at": "2026-09-17T10:05:00+00:00"},
    ]


def test_reviews_degrade_without_provider() -> None:
    """通道未注入/调用失败 → 200 空列表 + 内存态口径说明（不旁路审批安全面）。"""
    module = _load_server()
    body = _body_json(_call(module, f"{_EP}/reviews"))
    assert body["items"] == []
    assert "重启即失" in body["note"] and "审批卡" in body["note"]

    async def _boom(payload: dict) -> dict:
        raise RuntimeError("sidecar down")

    module._set_provider("tool-executor", _boom)
    body = _body_json(_call(module, f"{_EP}/reviews"))
    assert body["items"] == [] and "重启即失" in body["note"]


# ── 写动作：派 issue（task_submit 真派发，fake 捕获断言 args 形状）────────────────

def test_dispatch_issue_posts_task_submit() -> None:
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-789"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/dispatch_issue", method="POST",
            raw_body=json.dumps({"issue_text": "登录接口 502，复现：并发 100 请求", "title": "修复登录 502"}),
        )
    )
    assert body == {"task_id": "task-789"}
    assert captured["tool_name"] == "task_submit" and captured["plugin_id"] == "task_submit_tool"
    args = captured["args"]
    assert args["target_type"] == "agent"
    assert args["target_id"] == "mode_coding/programming_orchestrator_agent_v2"
    # 面板按主 agent（L1）身份代用户派发（tool-executor 直调无注入链，须自携）
    assert args["parent_agent_level"] == 1
    assert args["mode"] == "coding" and args["task_kind"] == "coding_issue"
    assert args["goal_title"] == "修复登录 502"
    # goal_description = issue 文本 + 修复要求（长度上限 2000 与 task_submit 契约对齐）
    assert "登录接口 502" in args["goal_description"]
    assert "修复要求" in args["goal_description"]
    assert len(args["goal_description"]) <= 2000


def test_dispatch_issue_accepts_kernel_base64_body() -> None:
    """内核形态 body：http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入，
    动作必须正确解析（F1 防回退）；裸 JSON 直调形态同收（宿主桥之外的调用方）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-b64"}}

    module._set_provider("tool-executor", _fake_invoke)
    payload = {"issue_text": "内核 base64 形态的 issue", "title": "base64 形态标题"}
    raw_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    body = _body_json(
        _call(module, f"{_EP}/actions/dispatch_issue", method="POST", raw_body=raw_b64)
    )
    assert body == {"task_id": "task-b64"}
    args = captured["args"]
    assert args["goal_title"] == "base64 形态标题"
    assert "内核 base64 形态的 issue" in args["goal_description"]

    # 裸 JSON 形态（第二组区分度输入）：title 缺省 → 从 issue 文本派生
    captured.clear()
    _body_json(
        _call(
            module, f"{_EP}/actions/dispatch_issue", method="POST",
            raw_body=json.dumps({"issue_text": "裸 JSON 形态的 issue"}),
        )
    )
    args = captured["args"]
    assert args["goal_title"].startswith("编码修复·")
    assert "裸 JSON 形态的 issue" in args["goal_description"]


def test_dispatch_target_key_resolves_through_task_submit_chain() -> None:
    """F2 目标键合法性（真解析链，离线）：模式键经 task_submit 二级解析命中包内
    agent yaml，level L2/L3（闸门拒 L1 与缺失）、is_active——锁住键可派发性。"""
    tool_module = _load_task_submit_tool()
    config, corrupt = tool_module.TaskSubmitTool._load_agent_yaml_dict(
        "mode_coding/programming_orchestrator_agent_v2"
    )
    assert corrupt == ""
    assert isinstance(config, dict)
    assert config.get("level") in ("L2", "L3")
    assert config.get("is_active", True) is True


def _load_task_submit_tool():
    """装载 task_submit 工具模块（唯一实例），键合法性走真解析链。"""
    if "task_submit_tool_live_module" in sys.modules:
        return sys.modules["task_submit_tool_live_module"]
    shared_root = os.path.dirname(MODES_DIR)  # plugins/shared
    tool_dir = os.path.join(shared_root, "tools", "task_submit")
    for p in (shared_root, tool_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    path = os.path.join(tool_dir, "tool.py")
    spec = importlib.util.spec_from_file_location("task_submit_tool_live_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dispatch_issue_title_defaults_to_issue_text() -> None:
    """title 缺省 → goal_title 从 issue 文本派生（第二组区分度输入）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"id": "task-abc"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/dispatch_issue", method="POST",
            raw_body=json.dumps({"issue_text": "flaky 测试 test_retry 偶发超时"}),
        )
    )
    assert body == {"task_id": "task-abc"}
    args = captured["args"]
    assert args["target_id"] == "mode_coding/programming_orchestrator_agent_v2"
    assert args["goal_title"] == f"编码修复·{'flaky 测试 test_retry 偶发超时'[:40]}"


def test_dispatch_issue_error_paths() -> None:
    module = _load_server()
    # 缺 issue_text → 400；GET 打写动作端点 → 404
    assert _envelope_status(_call(module, f"{_EP}/actions/dispatch_issue", method="POST")) == 400
    assert _envelope_status(_call(module, f"{_EP}/actions/dispatch_issue")) == 404
    # 通道不可用 → 如实返回 error（不静默假成功）
    body = _body_json(
        _call(
            module, f"{_EP}/actions/dispatch_issue", method="POST",
            raw_body=json.dumps({"issue_text": "修 x"}),
        )
    )
    assert "error" in body and "tool-executor" in body["error"]


# ── 宿主融合：写动作 session_id 入对话框线程 + 面板主题 token 化 ─────────────────

def test_dispatch_issue_passes_session_id_through() -> None:
    """body 带 session_id（宿主 ctx.sync 对话框线程）→ task_submit args 原样透传。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-s1"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/dispatch_issue", method="POST",
            raw_body=json.dumps({"issue_text": "登录接口 502", "session_id": "sess-host-1"}),
        )
    )
    assert body == {"task_id": "task-s1"}
    assert captured["args"]["session_id"] == "sess-host-1"


def test_dispatch_issue_omits_session_id_when_absent() -> None:
    """无 session_id / 空串（面板未收到 ctx.sync）→ args 不带该键（无则省略）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-s2"}}

    module._set_provider("tool-executor", _fake_invoke)
    for raw in (
        json.dumps({"issue_text": "修 x"}),  # 键缺席
        json.dumps({"issue_text": "修 x", "session_id": ""}),  # 空串同缺席
    ):
        _body_json(
            _call(module, f"{_EP}/actions/dispatch_issue", method="POST", raw_body=raw)
        )
        assert "session_id" not in captured["args"]


def test_panel_html_host_fusion_contract() -> None:
    """面板 HTML：宿主下行桥接收器（theme.sync/ctx.sync）+ 主题 token 化（--ag-*）。"""
    path = os.path.join(MODES_DIR, "mode_coding", "webview", "coding_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "theme.sync" in html and "ctx.sync" in html
    assert "--ag-" in html and "__agentosCtx" in html


# ── 降级与路由边界 ────────────────────────────────────────────────────────────────

def test_no_provider_degrades_to_empty_payloads() -> None:
    """provider 未注入（单测/内核握手前）→ 200 空载荷（前端契约不破坏）。"""
    module = _load_server()
    assert _body_json(_call(module, f"{_EP}/sessions")) == {"sessions": []}
    board = _body_json(_call(module, f"{_EP}/board"))
    assert len(board["columns"]) == 6 and all(col["cards"] == [] for col in board["columns"])
    assert _body_json(_call(module, f"{_EP}/messages", query={"pipeline_id": "p-x"})) == {"messages": []}


def test_unrouted_paths_return_404() -> None:
    module = _load_server()
    assert _envelope_status(_call(module, "/ext/mode_coding/data/nope")) == 404
    assert _envelope_status(_call(module, f"{_EP}/sessions", method="POST")) == 404
    assert _envelope_status(_call(module, f"{_EP}/bootstrap", method="DELETE")) == 404


def test_bootstrap_serves_profile_metadata() -> None:
    module = _load_server()
    body = _body_json(_call(module, f"{_EP}/bootstrap"))
    assert body["mode"] == "coding" and body["panel_page_id"] == "coding_delivery"


def test_panel_html_bridge_error_banner_distinct_from_empty_state() -> None:
    """P1 桥失败 ≠ 真空数据：agentosFetch 失败渲染显式错误横幅（--ag-err 语义色
    + 重试按钮），与「暂无 XX」诚实空态视觉区分。"""
    path = os.path.join(MODES_DIR, "mode_coding", "webview", "coding_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "function bridgeErrorBox(" in html
    assert "面板桥不可用/加载失败" in html
    assert 'onclick="refreshAll()">重试' in html
    # 横幅样式走 --ag-err 语义色（var(--err, fallback) 桥接形式）
    assert ".bridge-err" in html and "var(--err, #dc2626)" in html
