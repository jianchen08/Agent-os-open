# @feature: FP-0.2.二 模式面板成熟化（godot 切片）| @ci: python-coverage
"""mode_godot 活面板行为测试：编辑器 seam 四态 / godot 记录双口径 / 写动作捕获。

面板数据面（ADR 2026-09-17-mode-panel-mature-interfaces）：
- 场景/预览读数走 _addon_get seam（宿主桥 addons/agentos 本地 HTTP 9600），测试
  monkeypatch seam 模拟在线/离线，不真连网；
- 执行记录走 pipeline-state 内核读 provider（mode=="godot" 或 agent_id 含 "godot"
  双口径过滤），未注入降级空载荷；
- 消息流 has_error 标注（godot_run 运行报错回灌的红显观测口径）；
- 写动作经 tool-executor fake provider 捕获 args（task_submit / godot_run），
  禁止真派发。
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

pytestmark = pytest.mark.unit


def _load_server():
    """按唯一模块名装载 mode_godot server.py（前缀 mode_live_mode_godot 防裸名互覆）。"""
    path = os.path.join(MODES_DIR, "mode_godot", "server.py")
    spec = importlib.util.spec_from_file_location("mode_live_mode_godot", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call(module, path: str, method: str = "GET", raw_body: str = "", query: dict | None = None) -> dict:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query or {})
    )


def _body_bytes(result: dict) -> bytes:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return base64.b64decode(data["body"])


def _body_json(result: dict) -> dict:
    return json.loads(_body_bytes(result).decode("utf-8"))


def _envelope_status(result: dict) -> int:
    """边界状态断言走 envelope data.status（HttpHandleResponse 契约）。"""
    return int(result["data"]["status"])


_EP = "/ext/mode_godot/data"


def _addon_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


# addon /health /status /context 真实返回形态（hosts/godot-addons/agentos/agentos_connector.gd）
_HEALTH = {"status": "ok", "version": "0.2.0"}
_STATUS = {
    "status": "ok", "version": "0.2.0", "engine": "godot",
    "engine_version": "4.4.1.stable", "project": "Breakout",
}
_CONTEXT = {
    "active_scene": "res://scenes/main.tscn",
    "selected_object": "Player",
    "scene_name": "main",
    "engine_version": "4.4.1.stable",
    "selected_objects": ["Player"],
    "selection_detail": [
        {
            "name": "Player", "type": "CharacterBody2D",
            "path": "/root/main/Player", "position": "(96, 128)",
            "preview_kind": "viewport",
        },
    ],
}

_OFFLINE_NOTE = "Godot 编辑器未连接（9600 无响应），点「打开 Godot 编辑器」拉起"


# ── 场景/预览 seam 四态（在线+离线 × scene/preview）──────────────────────────────

def test_scene_online_serves_probe_and_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_server()
    seen: list[str] = []

    async def _fake_addon_get(path: str) -> tuple[int, bytes]:
        seen.append(path)
        routes = {
            "/health": (200, _addon_json_bytes(_HEALTH)),
            "/status": (200, _addon_json_bytes(_STATUS)),
            "/context": (200, _addon_json_bytes(_CONTEXT)),
        }
        return routes[path]

    monkeypatch.setattr(module, "_addon_get", _fake_addon_get)
    body = _body_json(_call(module, f"{_EP}/scene"))
    assert body["editor_online"] is True and body["note"] == ""
    assert seen == ["/health", "/status", "/context"]  # 探活 → 工程 → 选中快照
    snap = body["snapshot"]
    assert snap["project"] == "Breakout"
    assert snap["scene_name"] == "main" and snap["active_scene"] == "res://scenes/main.tscn"
    assert snap["selected_object"] == "Player"
    assert snap["selection_detail"] == [
        {"name": "Player", "type": "CharacterBody2D", "path": "/root/main/Player", "position": "(96, 128)"}
    ]


def test_scene_offline_degrades_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主桥拒连（编辑器未开）→ editor_online:false + 拉起指引，不假数据。"""
    module = _load_server()

    async def _fake_addon_get(path: str) -> tuple[int, bytes]:
        raise OSError("connection refused")

    monkeypatch.setattr(module, "_addon_get", _fake_addon_get)
    body = _body_json(_call(module, f"{_EP}/scene"))
    assert body == {"editor_online": False, "snapshot": None, "note": _OFFLINE_NOTE}


def test_preview_online_returns_png_data_url(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_server()
    png = b"\x89PNG-fake-bytes-0"
    calls: list[str] = []

    async def _fake_addon_get(path: str) -> tuple[int, bytes]:
        calls.append(path)
        return 200, png

    monkeypatch.setattr(module, "_addon_get", _fake_addon_get)
    body = _body_json(_call(module, f"{_EP}/preview"))
    assert calls == ["/selection/preview?index=0"]
    assert body["editor_online"] is True and body["note"] == ""
    assert body["preview_data_url"] == (
        "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    )


def test_preview_offline_degrades_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_server()

    async def _fake_addon_get(path: str) -> tuple[int, bytes]:
        raise TimeoutError("addon probe timed out")

    monkeypatch.setattr(module, "_addon_get", _fake_addon_get)
    body = _body_json(_call(module, f"{_EP}/preview"))
    assert body == {"editor_online": False, "preview_data_url": None, "note": _OFFLINE_NOTE}


def test_preview_404_is_online_without_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """在线但选中节点无预览（404）→ 在线事实与无预览事实分开报。"""
    module = _load_server()

    async def _fake_addon_get(path: str) -> tuple[int, bytes]:
        return 404, _addon_json_bytes({"error": "no preview for index 0"})

    monkeypatch.setattr(module, "_addon_get", _fake_addon_get)
    body = _body_json(_call(module, f"{_EP}/preview"))
    assert body["editor_online"] is True
    assert body["preview_data_url"] is None
    assert "无预览" in body["note"]


# ── 执行记录（godot 双口径过滤）+ 消息流（has_error 红显口径）────────────────────

def _fake_state_rows() -> list[dict]:
    return [
        {  # mode 口径：mode=="godot"（mode_godot/ 命名空间 agent 键）
            "pipeline_id": "pipe-g1", "thread_id": "t1",
            "agent_id": "mode_godot/godot_orchestrator_agent",
            "run_status": "running", "mode": "godot", "task.goal": "重做主场景布局",
            "task.status": "running", "message_count": 6,
        },
        {  # agent_id 口径：mode 键缺失但 agent_id 含 "godot"（非模式命名空间键）
            "pipeline_id": "pipe-g2", "thread_id": "t2",
            "agent_id": "executor/godot_expert",
            "run_status": "completed", "mode": "", "task.goal": "给敌方飞机加受击闪白",
            "task.status": "completed", "message_count": 3,
        },
        {  # 他模式行：必须被过滤
            "pipeline_id": "pipe-x", "thread_id": "t3", "agent_id": "main",
            "run_status": "completed", "mode": "coding", "task.goal": "修 bug",
            "message_count": 9,
        },
    ]


def test_records_filter_matches_both_godot_calibers() -> None:
    module = _load_server()
    module._set_provider(
        "pipeline-state", lambda: asyncio.sleep(0, result=_fake_state_rows())
    )
    records = _body_json(_call(module, f"{_EP}/records"))["records"]
    assert [r["pipeline_id"] for r in records] == ["pipe-g2", "pipe-g1"]  # 倒序新会话在前
    by_id = {r["pipeline_id"]: r for r in records}
    assert by_id["pipe-g1"]["goal"] == "重做主场景布局"
    assert by_id["pipe-g1"]["agent_id"] == "mode_godot/godot_orchestrator_agent"
    assert by_id["pipe-g2"]["goal"] == "给敌方飞机加受击闪白"
    assert by_id["pipe-g2"]["message_count"] == 3
    assert all(r["pipeline_id"] != "pipe-x" for r in records)


def test_records_and_messages_degrade_to_empty_without_kernel() -> None:
    """provider 未注入（内核握手前）→ 200 空载荷（前端诚实空态契约不破坏）。"""
    module = _load_server()
    assert _body_json(_call(module, f"{_EP}/records")) == {"records": []}
    body = _body_json(_call(module, f"{_EP}/messages", query={"pipeline_id": "pipe-g1"}))
    assert body == {"messages": []}


def test_messages_mark_error_content_for_red_highlight() -> None:
    module = _load_server()

    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        assert pipeline_id == "pipe-g1"
        return [
            {
                "role": "assistant",
                "content_preview": "scene.play 后编辑器报 ERROR: res://player.gd(12) 解析错误",
                "status": "success", "created_at": "t1",
            },
            {"role": "assistant", "content_preview": "场景保存完成", "status": "success", "created_at": "t2"},
        ]

    module._set_provider("messages", _provider)
    msgs = _body_json(_call(module, f"{_EP}/messages", query={"pipeline_id": "pipe-g1"}))["messages"]
    assert msgs[0]["has_error"] is True  # 大写 ERROR 命中
    assert msgs[1]["has_error"] is False


def test_messages_query_contract() -> None:
    module = _load_server()
    # 缺 pipeline_id → 400；错误 method → 404
    assert _envelope_status(_call(module, f"{_EP}/messages")) == 400
    assert _envelope_status(_call(module, f"{_EP}/messages", method="POST")) == 404


# ── 写动作（fake tool-executor 捕获 args，不真派发）───────────────────────────────

def test_dispatch_action_captures_task_submit_args() -> None:
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-g1"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/dispatch", method="POST",
            raw_body=json.dumps({"instruction": "给 Player 加二段跳"}),
        )
    )
    assert body == {"task_id": "task-g1"}
    assert captured["tool_name"] == "task_submit" and captured["plugin_id"] == "task_submit_tool"
    args = captured["args"]
    assert args["target_type"] == "agent"
    assert args["target_id"] == "mode_godot/godot_orchestrator_agent"
    # 面板按主 agent（L1）身份代用户派发（tool-executor 直调无注入链，须自携）
    assert args["parent_agent_level"] == 1
    assert args["mode"] == "godot" and args["task_kind"] == "godot_scene_edit"
    assert "给 Player 加二段跳" in args["goal_title"]
    assert "给 Player 加二段跳" in args["goal_description"]
    # 选中引用权威定位口径随 goal 注入（godot 链修改目标定位契约）
    assert '消息中 <reference source="godot"> 选中节点为修改目标权威定位' in args["goal_description"]


def test_dispatch_accepts_kernel_base64_body() -> None:
    """内核形态 body：http_dispatcher 把请求原始字节 base64 编码后作 raw_body 传入，
    动作必须正确解析（F1 防回退）；裸 JSON 直调形态同收（宿主桥之外的调用方）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-b64"}}

    module._set_provider("tool-executor", _fake_invoke)
    raw_b64 = base64.b64encode(
        json.dumps({"instruction": "内核 base64 形态：给主菜单加暂停功能"}).encode("utf-8")
    ).decode("ascii")
    body = _body_json(_call(module, f"{_EP}/actions/dispatch", method="POST", raw_body=raw_b64))
    assert body == {"task_id": "task-b64"}
    assert "内核 base64 形态：给主菜单加暂停功能" in captured["args"]["goal_description"]
    assert captured["args"]["target_id"] == "mode_godot/godot_orchestrator_agent"

    # 裸 JSON 形态（第二组区分度输入）
    captured.clear()
    _body_json(
        _call(
            module, f"{_EP}/actions/dispatch", method="POST",
            raw_body=json.dumps({"instruction": "裸 JSON 形态：调低阴影质量"}),
        )
    )
    assert "裸 JSON 形态：调低阴影质量" in captured["args"]["goal_description"]


def test_dispatch_missing_instruction_returns_400() -> None:
    module = _load_server()
    result = _call(module, f"{_EP}/actions/dispatch", method="POST")
    assert _envelope_status(result) == 400
    assert "instruction" in _body_json(result)["error"]


def test_dispatch_session_id_passthrough_to_dialog_thread() -> None:
    """宿主 ctx.sync 会话上下文随 dispatch 透传 → task_submit args 带 session_id
    （写动作送入对话框线程，ADR 宿主融合）。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-g3"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_EP}/actions/dispatch", method="POST",
            raw_body=json.dumps({"instruction": "给主菜单加返回按钮", "session_id": "sess_live_g1"}),
        )
    )
    assert body == {"task_id": "task-g3"}
    assert captured["tool_name"] == "task_submit"
    assert captured["args"]["session_id"] == "sess_live_g1"


def test_dispatch_without_session_id_omits_key() -> None:
    """无宿主会话上下文（置顶小窗独立打开等）→ 缺省不透传空串。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-g4"}}

    module._set_provider("tool-executor", _fake_invoke)
    _body_json(
        _call(
            module, f"{_EP}/actions/dispatch", method="POST",
            raw_body=json.dumps({"instruction": "调分辨率"}),
        )
    )
    assert "session_id" not in captured["args"]


def test_open_editor_invokes_godot_run_channel() -> None:
    """「打开 Godot 编辑器」= godot_run 探活命令（engine.commands），
    未开时 godot_run 按 GODOT_EDITOR_BIN 自动拉起——原软件拉起链。"""
    module = _load_server()
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"commands": {"scene": ["tree", "play"], "engine": ["commands"]}}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(_call(module, f"{_EP}/actions/open_editor", method="POST", raw_body="{}"))
    assert captured["tool_name"] == "godot_run" and captured["plugin_id"] == "godot_mcp"
    assert captured["args"]["method"] == "engine.commands"
    assert body["result"] == {"commands": {"scene": ["tree", "play"], "engine": ["commands"]}}


def test_open_editor_without_channel_returns_honest_error() -> None:
    module = _load_server()
    body = _body_json(_call(module, f"{_EP}/actions/open_editor", method="POST", raw_body="{}"))
    assert "error" in body and "tool-executor" in body["error"]


# ── 路由边界 + manifest 契约 ─────────────────────────────────────────────────────

def test_unrouted_and_wrong_method_404() -> None:
    module = _load_server()
    assert _envelope_status(_call(module, "/ext/mode_godot/data/unknown")) == 404
    assert _envelope_status(_call(module, f"{_EP}/actions/dispatch")) == 404  # GET 打写端点
    assert _envelope_status(_call(module, f"{_EP}/records", method="POST")) == 404
    assert _envelope_status(_call(module, f"{_EP}/scene", method="DELETE")) == 404


def test_bootstrap_serves_profile_metadata() -> None:
    module = _load_server()
    body = _body_json(_call(module, f"{_EP}/bootstrap"))
    assert body["mode"] == "godot" and body["panel_page_id"] == "godot_dev"
    assert body["chain"]["entry"] == "main"


def test_manifest_declares_desktop_widget_and_bumped_version() -> None:
    """置顶小窗声明（编辑器全屏干活 + 角落置顶 AI 面板）+ 种子升级版本号。"""
    path = os.path.join(MODES_DIR, "mode_godot", "plugin.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["version"] == "1.2.0"
    page = next(p for p in manifest["contributes"]["pages"] if p["id"] == "godot_dev")
    detachable = page["detachable"]
    assert detachable["desktopWidget"] is True
    assert detachable["popout"] is True and detachable["childWindow"] is True


def test_panel_html_host_fusion_bridge_and_tokens() -> None:
    """宿主融合契约：下行桥接收器（theme.sync→token 逐键 / ctx.sync→__agentosCtx）
    + 配色全跟宿主 --ag-* token（现值仅作未同步 fallback）。"""
    path = os.path.join(MODES_DIR, "mode_godot", "webview", "godot_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "theme.sync" in html and "ctx.sync" in html
    assert "__agentosCtx" in html
    assert "document.documentElement.style.setProperty" in html  # token 逐键落 documentElement
    assert "--ag-" in html and "--ag-bg" in html and "--ag-radius" in html
    # 面板 JS 带会话上下文派发（写动作入对话框线程）
    assert "session_id: window.__agentosCtx" in html


def test_panel_html_bridge_error_banner_distinct_from_empty_state() -> None:
    """P1 桥失败 ≠ 真空数据：agentosFetch 失败渲染显式错误横幅（--ag-err 语义色
    + 重试按钮），与「暂无 XX」诚实空态视觉区分。"""
    path = os.path.join(MODES_DIR, "mode_godot", "webview", "godot_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    assert "function bridgeErrorBox(" in html
    assert "面板桥不可用/加载失败" in html
    assert 'onclick="refreshAll()">重试' in html
    # 横幅样式走 --ag-err 语义色（var(--err, fallback) 桥接形式）
    assert ".bridge-err" in html and "var(--err, #dc2626)" in html
