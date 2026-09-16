# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
# -*- coding: utf-8 -*-
"""五模式 webview 面板页供给测试：http.handle 路由 / manifest 声明 / HTML 自包含。

面板承载形态（模式体系落地设计 §2 末，2026-09-15 用户裁定）：工作区 tab 页 =
插件自带 webview 页，经 http_endpoints（handler_capability=http.handle）按 path
供给包内 webview/ 单文件 HTML。仓内先例 = monitoring payload_diag 页。
同构种子按插件参数化逐份断言（与 test_mode_seeds 同一行为契约形态）。
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
]

# 各页信息架构锚点（设计稿 §3 对应小节的区块标题，HTML 内容断言用）
PANEL_MARKERS = {
    "coding": ("修复流水线", "diff 复盘", "worktree"),
    "writing": ("作品树", "章节编辑器", "设定集"),
    "roleplay": ("角色卡", "会话", "世界书"),
    "research": ("调研任务", "报告阅读器", "信源库"),
    "godot": ("godot 项目", "场景状态", "执行记录"),
}

pytestmark = pytest.mark.unit


def _load_server(plugin_id: str):
    """按唯一模块名装载各插件 server.py（与 test_mode_seeds 同形态，防裸名互覆）。"""
    path = os.path.join(MODES_DIR, plugin_id, "server.py")
    spec = importlib.util.spec_from_file_location(f"mode_panel_{plugin_id}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call(module, path: str, method: str = "GET") -> dict:
    return asyncio.run(module.http_handle(path=path, method=method))


def _body_json(result: dict) -> dict:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


@pytest.mark.parametrize("plugin_id,mode,page_id", SEEDS)
def test_panel_page_served_as_html(plugin_id: str, mode: str, page_id: str) -> None:
    module = _load_server(plugin_id)
    result = _call(module, f"/ext/{plugin_id}/page/{mode}-panel")
    assert result["success"] is True
    data = result["data"]
    assert data["status"] == 200
    assert data["headers"]["Content-Type"] == "text/html; charset=utf-8"
    html = base64.b64decode(data["body"]).decode("utf-8")
    assert html.startswith("<!DOCTYPE html>")
    for marker in PANEL_MARKERS[mode]:
        assert marker in html, f"{plugin_id}: 缺信息架构区块 {marker}"
    # 占位口径与被退役的 React 骨架同款：数据源缺口显式标注，不假实现
    assert "数据源缺口" in html


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


@pytest.mark.parametrize("plugin_id,mode,page_id", SEEDS)
def test_panel_html_is_self_contained(plugin_id: str, mode: str, page_id: str) -> None:
    """宿主 CSP（default-src 'none' + connect-src 'none'）约束面板页必须自包含：
    禁外链资源、禁脚本拉取；深浅色经 CSS 变量 + prefers-color-scheme 承载。"""
    path = os.path.join(MODES_DIR, plugin_id, "webview", f"{mode}_panel.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    lowered = html.lower()
    for banned in ("<script", "src=", "href=", "http://", "https://", "fetch(", "url("):
        assert banned not in lowered, f"{plugin_id}: 面板页含被 CSP 禁止的引用 {banned}"
    assert "prefers-color-scheme" in lowered
    assert "--bg" in lowered


@pytest.mark.parametrize("plugin_id,mode,page_id", SEEDS)
def test_manifest_declares_webview_page_and_endpoint(
    plugin_id: str, mode: str, page_id: str
) -> None:
    with open(os.path.join(MODES_DIR, plugin_id, "plugin.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    # 工作区 tab 条目改判 webview 形态：widget/props 契约对齐 WebviewWidget 容器
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
    # 选项追加声明（select-option → task_mode 选择器；面板页携 mode 配对键）
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
