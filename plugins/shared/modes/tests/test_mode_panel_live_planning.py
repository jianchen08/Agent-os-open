# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""计划模式活面板行为测试：数据端点归一/两态项目口径/写动作真派发形状。

面板成熟化（ADR 2026-09-17-mode-panel-mature-interfaces）：/data/* 数据端点 +
写动作经 tool-executor 真派发。本文件以 fake provider 断言归一形状与派发 args
（不真派发任务、不碰真实登记簿）；端点 manifest/页面桥契约见
test_mode_panel_pages.py（planning 已在 LIVE_PANELS）。

两态口径（server.py 调查结论）：task_service 无 projects 读服务（登记簿仅
HTTP 端点）→ 无 "projects" provider 时 /data/projects 从 pipeline-state 行按
task.parent_project_id 派生并带 note；provider 注入即登记态（未来服务落地通道）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_ID = "mode_planning"
_EP = f"/ext/{_PLUGIN_ID}/data"

pytestmark = pytest.mark.unit


def _load_server():
    """按唯一模块名装载 server.py（与 pages/seeds 测试模块名不同域，provider 态隔离）。"""
    path = os.path.join(MODES_DIR, _PLUGIN_ID, "server.py")
    spec = importlib.util.spec_from_file_location("mode_live_mode_planning", path)
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
    return int(result["data"]["status"])


def _state_provider(rows: list[dict]):
    async def _fn() -> list[dict]:
        return rows

    return _fn


# ── fake 数据（有区分度的 ≥2 组输入：planning/他模式/无挂靠/多项目）────────────────

def _fake_state_rows() -> list[dict]:
    return [
        {  # planning 会话行：挂靠 proj_r1，含 task.id
            "pipeline_id": "pipe-plan", "thread_id": "th-1", "agent_id": "main",
            "run_status": "running", "mode": "planning", "task.goal": "雾镇二期方案",
            "task.status": "running", "task.id": "t_plan",
            "task.parent_project_id": "proj_r1", "message_count": 6,
        },
        {  # 他模式任务行：不挂 mode 过滤（项目锚跨模式），挂靠 proj_x
            "pipeline_id": "pipe-code", "thread_id": "th-2", "agent_id": "main",
            "run_status": "completed", "mode": "coding", "task.goal": "修存档 bug",
            "task.status": "completed", "task.id": "t_code",
            "task.parent_project_id": "proj_x", "message_count": 12,
        },
        {  # planning 行但未挂靠项目：不入任何项目组
            "pipeline_id": "pipe-free", "thread_id": "th-3", "agent_id": "main",
            "run_status": "completed", "mode": "planning", "task.goal": "闲聊",
            "task.status": "completed", "message_count": 1,
        },
    ]


def _fake_messages() -> list[dict]:
    return [
        {"role": "user", "content_preview": "给雾镇做二期方案", "status": "success", "created_at": "t1"},
        {"role": "assistant", "content_preview": "方案总纲如下……", "status": "success", "created_at": "t2"},
    ]


# ── 数据端点：降级 / 归一 / 查询契约 ─────────────────────────────────────────────

def test_live_planning_degrades_to_empty_without_kernel() -> None:
    """provider 未注入（单测/内核握手前）→ 200 空载荷（前端契约不破坏）。"""
    module = _load_server()
    assert _body_json(_call(module, f"{_EP}/sessions")) == {"sessions": []}
    assert _body_json(_call(module, f"{_EP}/tasks", query={"parent_task_id": "t1"})) == {"tasks": []}
    assert _body_json(
        _call(module, f"{_EP}/discussions", query={"pipeline_id": "pipe-x"})
    ) == {"messages": []}
    projects = _body_json(_call(module, f"{_EP}/projects"))
    assert projects["projects"] == [] and projects["source"] == "derived"
    assert "派生" in projects["note"] and "task.parent_project_id" in projects["note"]


def test_live_planning_bootstrap_and_sessions() -> None:
    module = _load_server()
    bootstrap = _body_json(_call(module, f"{_EP}/bootstrap"))
    assert bootstrap["mode"] == "planning" and bootstrap["panel_page_id"] == "planning_desk"

    module._set_provider("pipeline-state", _state_provider(_fake_state_rows()))
    sessions = _body_json(_call(module, f"{_EP}/sessions"))["sessions"]
    assert len(sessions) == 2  # mode=planning 过滤（coding 行出局）
    plan = next(s for s in sessions if s["pipeline_id"] == "pipe-plan")
    assert plan["goal"] == "雾镇二期方案" and plan["task_status"] == "running"
    assert plan["task_id"] == "t_plan" and plan["parent_project_id"] == "proj_r1"
    assert plan["message_count"] == 6


def test_live_planning_projects_derived_state() -> None:
    """无登记 provider（现状：task_service 无 projects 服务）→ state 行派生 + note。"""
    module = _load_server()
    module._set_provider("pipeline-state", _state_provider(_fake_state_rows()))
    body = _body_json(_call(module, f"{_EP}/projects"))
    assert body["source"] == "derived" and "登记" in body["note"]
    by_id = {p["project_id"]: p for p in body["projects"]}
    assert set(by_id) == {"proj_r1", "proj_x"}  # 未挂靠行不成组
    r1 = by_id["proj_r1"]
    assert r1["source"] == "derived"
    # 诚实不造假：登记簿字段派生口径不可得 → 留空不编造
    assert r1["workflow_state"] == "" and r1["auto_execute"] is None
    assert r1["tasks"] == [
        {"task_id": "t_plan", "pipeline_id": "pipe-plan", "title": "雾镇二期方案", "status": "running"}
    ]
    assert by_id["proj_x"]["tasks"][0]["task_id"] == "t_code"  # 项目锚跨模式


def test_live_planning_projects_registry_state() -> None:
    """登记 provider 注入（未来 projects 读服务通道）→ 登记态归一 + 任务合并。"""
    module = _load_server()
    module._set_provider("pipeline-state", _state_provider(_fake_state_rows()))

    async def _registry() -> list[dict]:
        return [
            {"id": "proj_r1", "title": "雾镇", "workflow_state": "plan",
             "auto_execute": True, "status": "active"},
            {"id": "proj_empty", "title": "空项目", "workflow_state": "running",
             "auto_execute": False, "status": "active"},
        ]

    module._set_provider("projects", _registry)
    body = _body_json(_call(module, f"{_EP}/projects"))
    assert body["source"] == "registry" and "note" not in body
    by_id = {p["project_id"]: p for p in body["projects"]}
    assert set(by_id) == {"proj_r1", "proj_empty"}  # 登记簿为项目清单真值
    r1 = by_id["proj_r1"]
    assert r1["title"] == "雾镇" and r1["workflow_state"] == "plan"
    assert r1["auto_execute"] is True
    assert [t["task_id"] for t in r1["tasks"]] == ["t_plan"]  # state 行任务并入
    assert by_id["proj_empty"]["tasks"] == []  # 登记后尚无子任务行 → 空组可见


def test_live_planning_task_tree_two_levels() -> None:
    """任务链：/data/tasks 单层查询归一成树行；前端按 parent 逐层递归组树。"""
    module = _load_server()
    captured: list[str] = []

    async def _task_list(parent_task_id: str) -> dict:
        captured.append(parent_task_id)
        if parent_task_id == "t_plan":
            return {"tasks": [
                {"id": "t_arch", "title": "架构设计", "status": "completed", "priority": 1},
                {"id": "t_impl", "title": "编码实现", "status": "running", "priority": 2},
            ], "total": 2}
        assert parent_task_id == "t_arch"
        return {"tasks": [
            {"id": "t_arch_doc", "title": "方案总纲文档", "status": "completed", "priority": 3},
        ], "total": 1}

    module._set_provider("task-list", _task_list)
    level1 = _body_json(_call(module, f"{_EP}/tasks", query={"parent_task_id": "t_plan"}))["tasks"]
    assert level1 == [
        {"task_id": "t_arch", "title": "架构设计", "status": "completed", "children": []},
        {"task_id": "t_impl", "title": "编码实现", "status": "running", "children": []},
    ]
    level2 = _body_json(_call(module, f"{_EP}/tasks", query={"parent_task_id": "t_arch"}))["tasks"]
    assert level2[0]["task_id"] == "t_arch_doc"
    assert captured == ["t_plan", "t_arch"]  # 逐层拉取传参如实


def test_live_planning_discussions_query_contract() -> None:
    module = _load_server()

    async def _messages(pipeline_id: str, limit: int | None = None) -> list[dict]:
        captured["pipeline_id"] = pipeline_id
        return _fake_messages()

    captured: dict = {}
    module._set_provider("messages", _messages)
    body = _body_json(_call(module, f"{_EP}/discussions", query={"pipeline_id": "pipe-plan"}))
    assert captured["pipeline_id"] == "pipe-plan"
    assert body["messages"][0] == {
        "role": "user", "content": "给雾镇做二期方案", "status": "success", "created_at": "t1",
    }
    assert body["messages"][1]["role"] == "assistant"


# ── 写动作：真派发形状捕获（fake tool-executor，不真派发）────────────────────────

def test_live_planning_create_project_action_invokes_tool() -> None:
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"project_id": "proj_9", "title": "雾镇", "path": "D:/ws/projects/雾镇",
                         "workflow_state": "plan", "created": True, "init": {"committed": True}}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/create_project", method="POST",
            raw_body=json.dumps({"goal": "雾镇", "path": "D:/ws/projects/雾镇", "project_type": "godot"}),
        )
    )
    assert captured["tool_name"] == "project_create"
    assert captured["plugin_id"] == "project_create_tool"
    assert captured["args"] == {
        "goal": "雾镇", "path": "D:/ws/projects/雾镇", "project_type": "godot",
    }
    assert body == {"project_id": "proj_9", "title": "雾镇", "path": "D:/ws/projects/雾镇",
                    "workflow_state": "plan", "created": True}


def test_live_planning_create_project_optional_args_omitted() -> None:
    """path/project_type 缺省不透传空串（project_create schema 默认值自持）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"project_id": "proj_a", "created": False}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(module, f"{_EP}/actions/create_project", method="POST",
              raw_body=json.dumps({"goal": "雾镇"}))
    )
    assert captured["args"] == {"goal": "雾镇"}
    assert body["project_id"] == "proj_a" and body["created"] is False


def test_live_planning_plan_action_dispatches_task_submit() -> None:
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task_7"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(module, f"{_EP}/actions/plan", method="POST",
              raw_body=json.dumps({"goal": "给雾镇做二期方案"}))
    )
    assert body == {"task_id": "task_7"}
    assert captured["tool_name"] == "task_submit" and captured["plugin_id"] == "task_submit_tool"
    args = captured["args"]
    assert args["target_type"] == "agent"
    assert args["target_id"] == "executor/generation/research_agent"
    # 面板按主 agent（L1）身份代用户派发（tool-executor 直调无注入链，须自携）
    assert args["parent_agent_level"] == 1
    assert args["mode"] == "planning" and args["task_kind"] == "planning_plan"
    assert "给雾镇做二期方案" in args["goal_description"]
    assert len(args["goal_description"]) <= 2000  # task_submit schema 上限
    assert args["goal_title"].startswith("方案规划")


def test_live_planning_plan_accepts_kernel_base64_body() -> None:
    """内核形态 body：http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入，
    动作必须正确解析（F1 防回退）；裸 JSON 直调形态同收（宿主桥之外的调用方）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task_b64"}}

    module._set_provider("tool-executor", _fake_invoke)
    raw_b64 = base64.b64encode(
        json.dumps({"goal": "内核 base64 形态的规划目标"}).encode("utf-8")
    ).decode("ascii")
    body = _body_json(_call(module, f"{_EP}/actions/plan", method="POST", raw_body=raw_b64))
    assert body == {"task_id": "task_b64"}
    assert "内核 base64 形态的规划目标" in captured["args"]["goal_description"]
    assert captured["args"]["target_id"] == "executor/generation/research_agent"

    # 裸 JSON 形态（第二组区分度输入）
    captured.clear()
    _body_json(
        _call(module, f"{_EP}/actions/plan", method="POST",
              raw_body=json.dumps({"goal": "裸 JSON 形态的规划目标"}))
    )
    assert "裸 JSON 形态的规划目标" in captured["args"]["goal_description"]


def test_plan_target_key_resolves_through_task_submit_chain() -> None:
    """F2 目标键合法性（真解析链，离线）：executor_pool 首键经 task_submit 磁盘
    rglob 命中配置，level L2/L3（闸门拒 L1 与缺失）、is_active——锁住键可派发性。"""
    tool_module = _load_task_submit_tool()
    config, corrupt = tool_module.TaskSubmitTool._load_agent_yaml_dict(
        "executor/generation/research_agent"
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


def test_live_planning_plan_action_session_id_passthrough() -> None:
    """宿主 ctx.sync 会话上下文随 plan 透传 → task_submit args 带 session_id
    （写动作送入对话框线程，ADR 宿主融合）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task_8"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/plan", method="POST",
            raw_body=json.dumps({"goal": "雾镇三期规划", "session_id": "sess_live_p1"}),
        )
    )
    assert body == {"task_id": "task_8"}
    assert captured["tool_name"] == "task_submit"
    assert captured["args"]["session_id"] == "sess_live_p1"


def test_live_planning_plan_action_without_session_id_omits_key() -> None:
    """无宿主会话上下文 → 缺省不透传空串（create_project 无会话语义不受影响）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task_9"}}

    module._set_provider("tool-executor", _fake_invoke)
    _body_json(
        _call(module, f"{_EP}/actions/plan", method="POST", raw_body=json.dumps({"goal": "x"}))
    )
    assert "session_id" not in captured["args"]


def test_live_planning_action_error_paths() -> None:
    module = _load_server()
    # 缺 goal → 400（两动作同参契约）
    assert _envelope_status(_call(module, f"{_EP}/actions/plan", method="POST")) == 400
    assert _envelope_status(
        _call(module, f"{_EP}/actions/create_project", method="POST",
              raw_body=json.dumps({"path": "D:/x"}))
    ) == 400
    # GET 打写动作端点 → 404；未路由数据 path → 404
    assert _envelope_status(_call(module, f"{_EP}/actions/plan")) == 404
    assert _envelope_status(_call(module, f"{_EP}/nope")) == 404
    # 派发通道未注入 → 如实报错不假成功（409）
    result = _call(module, f"{_EP}/actions/plan", method="POST",
                   raw_body=json.dumps({"goal": "x"}))
    assert _envelope_status(result) == 409
    assert "派发通道不可用" in _body_json(result)["error"]
    # 上游工具失败（调用抛错）→ 409 + 错误透传
    async def _boom(payload: dict) -> dict:
        raise RuntimeError("invoke down")

    module._set_provider("tool-executor", _boom)
    result = _call(module, f"{_EP}/actions/plan", method="POST",
                   raw_body=json.dumps({"goal": "x"}))
    assert _envelope_status(result) == 409
    assert "task_submit 调用失败" in _body_json(result)["error"]


def test_live_planning_query_endpoints_missing_params() -> None:
    module = _load_server()
    assert _envelope_status(_call(module, f"{_EP}/tasks")) == 400
    assert _envelope_status(_call(module, f"{_EP}/discussions")) == 400
    # 已路由数据端点的错误 method → 404
    assert _envelope_status(_call(module, f"{_EP}/tasks", method="POST")) == 404
    assert _envelope_status(_call(module, f"{_EP}/projects", method="POST")) == 404


def test_live_planning_panel_html_host_fusion_bridge_and_tokens() -> None:
    """宿主融合契约：下行桥接收器（theme.sync→token 逐键 / ctx.sync→__agentosCtx）
    + 配色全跟宿主 --ag-* token（现值仅作未同步 fallback）。"""
    path = os.path.join(MODES_DIR, "mode_planning", "webview", "planning_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "theme.sync" in html and "ctx.sync" in html
    assert "__agentosCtx" in html
    assert "document.documentElement.style.setProperty" in html  # token 逐键落 documentElement
    assert "--ag-" in html and "--ag-bg" in html and "--ag-radius" in html
    # 仅「发起规划」带会话上下文；create_project 无会话语义不走此参
    assert html.count("session_id: window.__agentosCtx") == 1


def test_live_planning_panel_html_create_project_input_guard() -> None:
    """创建项目输入守卫：projGoal 带 type="text"（表单语义/自动化选择器可命中，
    无 type 属性曾致复验自动化 input[type=text] 未命中误判静默失败）+ 空值点击
    行内提示 + 创建成功/失败 toast——静默失败不再可能。"""
    path = os.path.join(MODES_DIR, "mode_planning", "webview", "planning_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert '<input id="projGoal" type="text"' in html
    # 空值点击：行内提示元素存在，createProject 守卫显示它、输入即撤
    assert 'id="projGoalHint"' in html
    guard_body = html.split("function createProject()", 1)[1].split("function planGoal()", 1)[0]
    assert "projGoalHint').style.display = 'block'" in guard_body
    assert "$('projGoal').addEventListener('input'" in html
    # 成功/失败 toast 反馈均在（「创建成功 toast」确认存在）
    assert "项目已" in html and "'创建' : '复用既有'" in html
    assert "创建失败" in html


def test_panel_html_bridge_error_banner_distinct_from_empty_state() -> None:
    """P1 桥失败 ≠ 真空数据：agentosFetch 失败渲染显式错误横幅（--ag-err 语义色
    + 重试按钮），与「暂无 XX」诚实空态视觉区分。"""
    path = os.path.join(MODES_DIR, "mode_planning", "webview", "planning_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "function bridgeErrorBox(" in html
    assert "面板桥不可用/加载失败" in html
    assert 'onclick="refreshAll()">重试' in html
    # 横幅样式走 --ag-err 语义色（var(--err, fallback) 桥接形式）
    assert ".bridge-err" in html and "var(--err, #dc2626)" in html
