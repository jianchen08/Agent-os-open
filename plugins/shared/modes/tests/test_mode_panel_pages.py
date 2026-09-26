# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""模式 webview 面板页供给测试：http.handle 路由 / manifest 声明 / 页面契约。

面板承载形态（模式体系落地设计 §2 末，2026-09-15 用户裁定）：工作区 tab 页 =
插件自带 webview 页，经 http_endpoints（handler_capability=http.handle）按 path
供给包内 webview/ 单文件 HTML。仓内先例 = monitoring payload_diag 页。

成熟化（ADR 2026-09-17-mode-panel-mature-interfaces，六包已全部迁移完成）：
活面板态 = 内联脚本 + window.agentos 桥取数 + /data/* 数据端点 + 写动作
（对标成熟软件界面；仍禁外链资源，CSP 同源约束）。骨架态（静态页 + 「数据源缺口」
占位）已成历史，契约由 git 历史承载。
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

# (plugin_id, mode, page_id)：page id 契约不变（mode.describe panel_page_id 同源）
SEEDS = [
    ("mode_coding", "coding", "coding_delivery"),
    ("mode_writing", "writing", "writing_workshop"),
    ("mode_roleplay", "roleplay", "roleplay_studio"),
    ("mode_research", "research", "research_desk"),
    ("mode_godot", "godot", "godot_dev"),
    ("mode_planning", "planning", "planning_desk"),
]

LIVE_PANELS = {"roleplay", "writing", "coding", "research", "godot", "planning"}

# 各页信息架构锚点（对标产品界面还原的区块标题，HTML 内容断言用）
PANEL_MARKERS = {
    "coding": ("修复流水线", "diff 复盘", "worktree"),
    "writing": ("作品树", "章节编辑器", "设定集"),
    "roleplay": ("角色卡", "会话", "世界书"),
    "research": ("研究任务", "报告阅读器", "信源库"),
    "godot": ("godot 项目", "场景状态", "执行记录"),
    "planning": ("项目状态", "方案讨论", "任务链"),
}

pytestmark = pytest.mark.unit


def _load_server(plugin_id: str):
    """按唯一模块名装载各插件 server.py（防裸名互覆；每次调用全新 provider 态）。"""
    path = os.path.join(MODES_DIR, plugin_id, "server.py")
    spec = importlib.util.spec_from_file_location(f"mode_panel_{plugin_id}", path)
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
    """边界状态断言走 envelope data.status（HttpHandleResponse 契约：HTTP 语义
    落信封，body 只带 {"error": 消息}，同 test_unrouted_request_returns_404）。"""
    return int(result["data"]["status"])


# ── 通用契约（六包同构）──────────────────────────────────────────────────────────

@pytest.mark.parametrize("plugin_id,mode,page_id", SEEDS)
def test_panel_page_served_as_html(plugin_id: str, mode: str, page_id: str) -> None:
    module = _load_server(plugin_id)
    result = _call(module, f"/ext/{plugin_id}/page/{mode}-panel")
    assert result["success"] is True
    data = result["data"]
    assert data["status"] == 200
    assert data["headers"]["Content-Type"] == "text/html; charset=utf-8"
    html = _body_bytes(result).decode("utf-8")
    assert html.startswith("<!DOCTYPE html>")
    for marker in PANEL_MARKERS[mode]:
        assert marker in html, f"{plugin_id}: 缺信息架构区块 {marker}"


@pytest.mark.parametrize("plugin_id,mode,page_id", SEEDS)
def test_panel_html_read_failure_returns_500(
    plugin_id: str, mode: str, page_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面板页文件缺失（OSError）→ 500 JSON 错误响应（边界翻译，不静默吐空页）。"""
    module = _load_server(plugin_id)
    monkeypatch.setattr(
        module, "_PANEL_HTML_PATH", os.path.join(MODES_DIR, "no_such_panel.html")
    )
    result = _call(module, f"/ext/{plugin_id}/page/{mode}-panel")
    assert result["success"] is False
    assert "panel html read failed" in result["error"]
    data = result["data"]
    assert data["status"] == 500
    assert data["headers"]["Content-Type"].startswith("application/json")
    assert "error" in json.loads(base64.b64decode(data["body"]).decode("utf-8"))


@pytest.mark.parametrize("plugin_id,mode,page_id", SEEDS)
@pytest.mark.parametrize("kind", ["unknown_path", "wrong_method"])
def test_unrouted_request_returns_404(
    plugin_id: str, mode: str, page_id: str, kind: str
) -> None:
    """未路由 path 与已路由 path 的错误 method 都 404（不静默吐面板页）。"""
    module = _load_server(plugin_id)
    if kind == "unknown_path":
        result = _call(module, f"/ext/{plugin_id}/page/does-not-exist")
    else:
        result = _call(module, f"/ext/{plugin_id}/page/{mode}-panel", method="POST")
    assert result["success"] is True
    data = result["data"]
    assert data["status"] == 404
    assert data["headers"]["Content-Type"].startswith("application/json")
    assert "error" in json.loads(base64.b64decode(data["body"]).decode("utf-8"))


# ── 内核 base64 契约（写动作解析层六包一致）─────────────────────────────────────

_PARSE_BODY_CASES = [
    pytest.param(
        base64.b64encode(json.dumps({"card_id": "card_x", "greeting_index": 1}).encode()).decode(),
        {"card_id": "card_x", "greeting_index": 1},
        id="base64_json",
    ),
    pytest.param(
        '{"issue_text": "fix bug", "deep": true}',
        {"issue_text": "fix bug", "deep": True},
        id="raw_json",
    ),
    pytest.param("", {}, id="empty_body"),
    pytest.param(base64.b64encode(b"<html>not json</html>").decode(), {}, id="base64_non_json"),
]


@pytest.mark.parametrize("plugin_id", [s[0] for s in SEEDS])
@pytest.mark.parametrize(("raw_body", "expected"), _PARSE_BODY_CASES)
def test_parse_body_two_form_contract(plugin_id: str, raw_body: str, expected: dict) -> None:
    """六包 _parse_body 双形态契约一致：先 base64（内核 http_dispatcher 把请求字节
    base64 后作 raw_body 传入）后裸 JSON（宿主桥 MCP 直调形态），两态都失败或
    顶层非 dict → {}（交缺参 400 路径）。

    回归锚 = 2026-09-18 面板战役 P0：用户空间陈旧副本的 _parse_body 只有裸
    json.loads，缺 base64 形态，内核转发的写动作全部解析为 {} → 缺参 400。
    本测试在仓内六包逐一锁定，防新包/改包回退成裸 json.loads。
    """
    module = _load_server(plugin_id)
    assert module._parse_body(raw_body) == expected


@pytest.mark.parametrize("plugin_id,mode,page_id", SEEDS)
def test_manifest_declares_webview_page_and_endpoint(
    plugin_id: str, mode: str, page_id: str
) -> None:
    with open(os.path.join(MODES_DIR, plugin_id, "plugin.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    # 工作区 tab 条目判 webview 形态：widget/props 契约对齐 WebviewWidget 容器
    pages = {p["id"]: p for p in manifest["contributes"]["pages"]}
    page = pages[page_id]
    assert page["widget"] == "webview"
    assert page["path"] == f"/p/{page_id}"
    assert page["space"] == "workspace" and page["slot"] == "tab"
    assert page["props"] == {
        "pluginId": plugin_id,
        "htmlPath": f"/page/{mode}-panel",
        "widgetId": page_id,
    }
    assert page["mode"] == mode
    ui_widgets = manifest.get("ui_schema", {}).get("widgets", [])
    if mode in ("coding", "writing", "roleplay", "research"):
        # 四键契约内的模式进任务模式选择器
        opt = next(w for w in ui_widgets if w["id"] == f"mode_opt_{mode}")
        assert opt["type"] == "select-option" and opt["space"] == "chat-input"
        assert opt["props"]["target"] == "task_mode" and opt["props"]["value"] == mode
    else:
        # godot 家族键在 TASK_MODES 四键契约之外，不入任务模式选择器（仅面板配对）
        assert not any(w.get("type") == "select-option" for w in ui_widgets)
    # http_endpoints：页面路由声明对齐 monitoring 页面类路由（timeout 5000 / 并发 4）
    endpoints = {e["path"]: e for e in manifest["http_endpoints"]}
    endpoint = endpoints[f"/ext/{plugin_id}/page/{mode}-panel"]
    assert endpoint["method"] == "GET"
    assert endpoint["auth"] == "user"
    assert endpoint["handler_capability"] == "http.handle"
    assert endpoint["timeout_ms"] == 5000
    assert endpoint["max_concurrency"] == 4


# ── 活面板态契约（桥取数 + 数据端点 + 写动作；先例 = monitoring 数据端点形态）──────

_LIVE_SEEDS = [s for s in SEEDS if s[1] in LIVE_PANELS]

# 数据/动作端点契约：(path 后缀, method)
LIVE_DATA_ENDPOINTS = {
    "roleplay": [
        ("/data/bootstrap", "GET"),
        ("/data/sessions", "GET"),
        ("/data/messages", "GET"),
        ("/data/cards", "GET"),
        ("/data/lorebooks", "GET"),
        ("/data/personas", "GET"),
        ("/data/actions/play", "POST"),
        ("/data/actions/regenerate", "POST"),
        ("/data/cards/save", "POST"),
        ("/data/cards/delete", "POST"),
        ("/data/cards/import", "POST"),
        ("/data/cards/export", "POST"),
        ("/data/lorebooks/save", "POST"),
        ("/data/lorebooks/delete", "POST"),
        ("/data/lorebooks/import", "POST"),
        ("/data/personas/save", "POST"),
        ("/data/personas/delete", "POST"),
    ],
    "writing": [
        ("/data/bootstrap", "GET"),
        ("/data/sessions", "GET"),
        ("/data/messages", "GET"),
        ("/data/works", "GET"),
        ("/data/chapter", "GET"),
        ("/data/bible", "GET"),
        ("/data/actions/chapter_act", "POST"),
    ],
    "coding": [
        ("/data/bootstrap", "GET"),
        ("/data/sessions", "GET"),
        ("/data/messages", "GET"),
        ("/data/board", "GET"),
        ("/data/reviews", "GET"),
        ("/data/actions/dispatch_issue", "POST"),
    ],
    "research": [
        ("/data/bootstrap", "GET"),
        ("/data/sessions", "GET"),
        ("/data/messages", "GET"),
        ("/data/report", "GET"),
        ("/data/sources", "GET"),
        ("/data/actions/start", "POST"),
        ("/data/actions/followup", "POST"),
    ],
    "godot": [
        ("/data/bootstrap", "GET"),
        ("/data/scene", "GET"),
        ("/data/preview", "GET"),
        ("/data/sessions", "GET"),
        ("/data/messages", "GET"),
        ("/data/records", "GET"),
        ("/data/actions/dispatch", "POST"),
        ("/data/actions/open_editor", "POST"),
    ],
    "planning": [
        ("/data/bootstrap", "GET"),
        ("/data/projects", "GET"),
        ("/data/sessions", "GET"),
        ("/data/tasks", "GET"),
        ("/data/discussions", "GET"),
        ("/data/actions/create_project", "POST"),
        ("/data/actions/plan", "POST"),
    ],
}


@pytest.mark.parametrize("plugin_id,mode,page_id", _LIVE_SEEDS)
def test_live_panel_html_bridge_contract(plugin_id: str, mode: str, page_id: str) -> None:
    """活面板页：内联脚本 + window.agentos 桥；仍禁外链资源（宿主 CSP 同源约束）。"""
    path = os.path.join(MODES_DIR, plugin_id, "webview", f"{mode}_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    lowered = html.lower()
    assert "window.agentos" in lowered and "__agentos_webview" in lowered
    assert "agentosfetch" in lowered  # 桥调用封装（宿主代发 REST 带 token）
    for banned in ("src=", "href=", "http://", "https://", "url(", "import(", "websocket"):
        assert banned not in lowered, f"{plugin_id}: 面板页含被 CSP 禁止的引用 {banned}"
    assert "prefers-color-scheme" in lowered
    assert "--bg" in lowered


@pytest.mark.parametrize("plugin_id,mode,page_id", _LIVE_SEEDS)
def test_live_manifest_declares_data_and_action_endpoints(
    plugin_id: str, mode: str, page_id: str
) -> None:
    with open(os.path.join(MODES_DIR, plugin_id, "plugin.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    endpoints = {e["path"]: e for e in manifest["http_endpoints"]}
    expected = LIVE_DATA_ENDPOINTS[mode]
    for suffix, method in expected:
        endpoint = endpoints[f"/ext/{plugin_id}{suffix}"]
        assert endpoint["method"] == method
        assert endpoint["auth"] == "user"
        assert endpoint["handler_capability"] == "http.handle"
        # 超时分档：GET 数据端点 5s；POST 写动作 20s——task_submit 经 tool-executor
        # 全链（解析/建工作空间）同步耗时超 5s 窗，动作端点 504 但任务已落库（假失败）。
        expected_timeout = 20000 if method == "POST" else 5000
        assert endpoint["timeout_ms"] == expected_timeout
        assert endpoint["max_concurrency"] == 4
    # detachable 悬浮窗契约（ADR 决策 4）：popout/childWindow 开 + 默认尺寸
    page = next(p for p in manifest["contributes"]["pages"] if p["id"] == page_id)
    detachable = page["detachable"]
    assert detachable["popout"] is True and detachable["childWindow"] is True
    assert set(detachable["defaultSize"]) == {"w", "h"}


# ── 活面板行为测试（fake provider 注入；不真派发任务）────────────────────────────

_ROLEPLAY_EP = "/ext/mode_roleplay/data"


def _fake_state_rows() -> list[dict]:
    return [
        {  # 本模式行：完整归一字段
            "pipeline_id": "pipe-aaa", "thread_id": "th-1", "agent_id": "mode_roleplay/card_luna",
            "run_status": "running", "mode": "roleplay", "task.goal": "与月语精灵夜谈",
            "task.status": "running", "message_count": 4,
        },
        {  # 他模式行：必须被过滤
            "pipeline_id": "pipe-bbb", "thread_id": "th-2", "agent_id": "main",
            "run_status": "completed", "mode": "coding", "task.goal": "修 bug",
            "message_count": 9,
        },
    ]


def _fake_messages() -> list[dict]:
    return [
        {"role": "user", "content_preview": "你好呀", "status": "success", "created_at": "t1"},
        {"role": "assistant", "content_preview": "*提灯照亮门廊*" * 3, "status": "success", "created_at": "t2"},
    ]


def test_live_roleplay_data_assets_served() -> None:
    module = _load_server("mode_roleplay")
    cards = _body_json(_call(module, f"{_ROLEPLAY_EP}/cards"))["cards"]
    assert len(cards) >= 3
    for card in cards:
        assert card["id"].startswith("card_")
        assert card["name"] and isinstance(card["first_mes"], str)
        assert isinstance(card["tags"], list) and isinstance(card["alternate_greetings"], list)
    # 出厂卡含缺 alternate_greetings 的形态（健壮渲染契约）
    assert any(not c["alternate_greetings"] for c in cards)

    books = _body_json(_call(module, f"{_ROLEPLAY_EP}/lorebooks"))["lorebooks"]
    assert len(books) >= 1
    entry = books[0]["entries"][0]
    assert entry["keys"] and entry["content"]
    assert isinstance(entry["enabled"], bool) and "insertion_order" in entry

    bootstrap = _body_json(_call(module, f"{_ROLEPLAY_EP}/bootstrap"))
    assert bootstrap["mode"] == "roleplay" and bootstrap["panel_page_id"] == "roleplay_studio"


def test_live_roleplay_sessions_degrade_to_empty_without_kernel() -> None:
    """provider 未注入（单测/内核握手前）→ 200 空载荷（前端契约不破坏）。"""
    module = _load_server("mode_roleplay")
    body = _body_json(_call(module, f"{_ROLEPLAY_EP}/sessions"))
    assert body == {"sessions": []}


def test_live_roleplay_sessions_filter_and_normalize() -> None:
    module = _load_server("mode_roleplay")
    module._set_provider("pipeline-state", lambda: asyncio.sleep(0, result=_fake_state_rows()))
    sessions = _body_json(_call(module, f"{_ROLEPLAY_EP}/sessions"))["sessions"]
    assert len(sessions) == 1
    row = sessions[0]
    assert row["pipeline_id"] == "pipe-aaa"
    assert row["goal"] == "与月语精灵夜谈" and row["task_status"] == "running"
    assert row["agent_id"] == "mode_roleplay/card_luna" and row["message_count"] == 4


def test_live_roleplay_messages_query_contract() -> None:
    module = _load_server("mode_roleplay")
    # 缺 pipeline_id → 400
    assert _envelope_status(_call(module, f"{_ROLEPLAY_EP}/messages")) == 400
    # 错误 method → 404
    assert _envelope_status(_call(module, f"{_ROLEPLAY_EP}/messages", method="POST")) == 404
    # 正常查询：归一 + provider 传参
    captured: dict = {}

    async def _provider(pipeline_id: str, limit: int | None = None) -> list[dict]:
        captured["pipeline_id"] = pipeline_id
        return _fake_messages()

    module._set_provider("messages", _provider)
    body = _body_json(
        _call(module, f"{_ROLEPLAY_EP}/messages", query={"pipeline_id": "pipe-aaa"})
    )
    assert captured["pipeline_id"] == "pipe-aaa"
    assert body["messages"][0] == {
        "role": "user", "content": "你好呀", "status": "success", "created_at": "t1",
    }
    assert body["messages"][1]["role"] == "assistant"


def test_live_roleplay_play_action_possess_only() -> None:
    """play 端点 possess-only 契约（fresh 开演已会话化：面板走 roleplay.continue
    宿主桥建扮演会话，不再派发任务）——校验卡 + 回执 presenter（含 theme），
    零任务派发。"""
    module = _load_server("mode_roleplay")
    captured: dict = {}

    async def _fake_invoke(payload: dict) -> dict:
        captured.update(payload)
        return {"data": {"task_id": "task-123"}}

    module._set_provider("tool-executor", _fake_invoke)
    body = _body_json(
        _call(
            module, f"{_ROLEPLAY_EP}/actions/play", method="POST",
            raw_body=json.dumps({"card_id": "card_luna", "play_mode": "possess"}),
        )
    )
    assert captured == {}, "play 不再派发任务（fresh 已会话化）"
    assert body["card_id"] == "card_luna" and body["play_mode"] == "possess"
    presenter = body["presenter"]
    assert presenter["card_id"] == "card_luna"
    assert presenter["name"] == "塞拉菲娜·月语" and presenter["avatar"] == "🌙"
    assert presenter["theme"]["id"] == "mode_roleplay_card_luna", "theme 档随 possess 下发"


def test_live_roleplay_play_action_error_paths() -> None:
    module = _load_server("mode_roleplay")
    # 缺 card_id → 400
    assert _envelope_status(_call(module, f"{_ROLEPLAY_EP}/actions/play", method="POST")) == 400
    # 未知卡 → 400 + 显式错误
    result = _call(
        module, f"{_ROLEPLAY_EP}/actions/play", method="POST",
        raw_body=json.dumps({"card_id": "card_nope"}),
    )
    assert _envelope_status(result) == 400
    assert "未知角色卡" in _body_json(result)["error"]
    # fresh 已删除（会话化，不留兼容层）→ 与未知值同判 400
    result = _call(
        module, f"{_ROLEPLAY_EP}/actions/play", method="POST",
        raw_body=json.dumps({"card_id": "card_luna", "play_mode": "fresh"}),
    )
    assert _envelope_status(result) == 400
    assert "未知 play_mode" in _body_json(result)["error"]
    # GET 打写动作端点 → 404
    assert _envelope_status(_call(module, f"{_ROLEPLAY_EP}/actions/play")) == 404


def test_live_roleplay_regenerate_retired_410() -> None:
    """regenerate 已收编进扮演会话的宿主消息操作 → 410 语义化退役。"""
    module = _load_server("mode_roleplay")
    result = _call(
        module, f"{_ROLEPLAY_EP}/actions/regenerate", method="POST",
        raw_body=json.dumps({"pipeline_id": "pipe-aaa"}),
    )
    assert _envelope_status(result) == 410
    assert "收编" in _body_json(result)["error"]


