# @feature: FP-0.2.二 snippet_box 片段速查库工具（manifest 声明契约/增删查/CSP 自包含供页） | @ci: python-coverage
"""snippet_box 单测：工具实现（多组区分输入）+ 声明一致性 + 页面 CSP 自包含 + 供页内联。"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path

import pytest
import snippet_box as impl

PLUGIN_DIR = Path(__file__).resolve().parent

# 禁串清单（mode_evolution 面板同款）：外链资源与出网通道在 CSP 下全部不可用。
_FORBIDDEN = re.compile(r"\bsrc=|\bhref=|url\(|fetch\(|websocket|XMLHttpRequest|import\(")


def _load_server():
    spec = importlib.util.spec_from_file_location("snippet_box_server", PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None  # server.py 随包在位，spec/loader 恒可解析
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(tmp_path) -> str:
    return str(tmp_path / "data" / "snippets.json")


# ---------- 工具面：add/search/list/delete ----------


def test_add_then_search_hits_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(impl, "DATA_PATH", _db(tmp_path))
    added = impl.snippet_box(action="add", title="Git 变基", tags="git, 版本控制", content="git rebase -i HEAD~3")
    assert added["ok"] is True
    assert added["snippets"][0]["tags"] == ["git", "版本控制"]  # 逗号分隔定形 + 去空格
    hit = impl.snippet_box(action="search", title="rebase")
    assert hit["ok"] is True  # 查询词只出现在 content
    assert len(hit["snippets"]) == 1
    assert hit["snippets"][0]["content"] == "git rebase -i HEAD~3"


def test_search_hits_tag_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(impl, "DATA_PATH", _db(tmp_path))
    impl.snippet_box(action="add", title="磁盘巡检", tags=["docker"], content="df -h")
    hit = impl.snippet_box(action="search", title="docker")
    assert len(hit["snippets"]) == 1  # 查询词只出现在 tag，不中 title/content


def test_search_case_insensitive_and_no_match(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(impl, "DATA_PATH", _db(tmp_path))
    impl.snippet_box(action="add", title="Python 虚拟环境", content="python -m venv .venv")
    hit = impl.snippet_box(action="search", title="VENV")
    assert len(hit["snippets"]) == 1  # 大小写不敏感子串
    miss = impl.snippet_box(action="search", title="kubernetes")
    assert miss["ok"] is True
    assert miss["snippets"] == []


def test_list_and_delete_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(impl, "DATA_PATH", _db(tmp_path))
    a = impl.snippet_box(action="add", title="t1", content="c1")
    b = impl.snippet_box(action="add", title="t2", content="c2")
    listing = impl.snippet_box(action="list")
    assert listing["ok"] is True
    assert len(listing["snippets"]) == 2
    assert [s["title"] for s in listing["snippets"]] == ["t1", "t2"]  # 插入序稳定
    gone = impl.snippet_box(action="delete", snippet_id=b["snippets"][0]["id"])
    assert gone["ok"] is True
    assert [s["id"] for s in impl.snippet_box(action="list")["snippets"]] == [a["snippets"][0]["id"]]
    ghost = impl.snippet_box(action="delete", snippet_id="nope")
    assert ghost["ok"] is False
    assert "nope" in ghost["message"]


def test_action_param_guards(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(impl, "DATA_PATH", _db(tmp_path))
    assert impl.snippet_box(action="add", title="只有标题")["ok"] is False  # add 缺 content
    assert impl.snippet_box(action="delete")["ok"] is False  # delete 缺 snippet_id
    assert impl.snippet_box(action="search")["ok"] is False  # search 缺查询词
    assert impl.snippet_box(action="bogus")["ok"] is False  # 未知 action
    assert impl.snippet_box(action="list")["snippets"] == []  # 全程未落任何数据


def test_output_envelope_and_schemas() -> None:
    assert set(impl.SNIPPET_BOX_OUTPUT_SCHEMA["required"]) == {"ok", "snippets", "message"}
    assert impl.SNIPPET_BOX_INPUT_SCHEMA["properties"]["action"]["enum"] == [
        "add",
        "search",
        "list",
        "delete",
    ]
    assert impl.SNIPPET_BOX_INPUT_SCHEMA["required"] == ["action"]
    # schema 本体必须是可序列化的合法 JSON 结构
    json.dumps(impl.SNIPPET_BOX_INPUT_SCHEMA)
    json.dumps(impl.SNIPPET_BOX_OUTPUT_SCHEMA)


# ---------- 页面面：CSP 自包含 + 声明结构 + 供页信封 ----------


def test_page_html_self_contained() -> None:
    html = (PLUGIN_DIR / "page.html").read_text(encoding="utf-8")
    assert len(re.findall(r"<script", html)) == 1
    assert not _FORBIDDEN.search(html)


def test_manifest_tool_and_page_declared() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    tools = manifest["capabilities"]["tools"]
    assert len(tools) == 1
    tool = tools[0]
    for key in ("name", "description", "input_schema", "output_schema", "render"):
        assert key in tool
    assert tool["input_schema"] == impl.SNIPPET_BOX_INPUT_SCHEMA  # G2 严格比对的静态预检
    assert tool["output_schema"] == impl.SNIPPET_BOX_OUTPUT_SCHEMA
    assert tool["render"] == impl.SNIPPET_BOX_RENDER
    pages = manifest["contributes"]["pages"]
    assert pages
    assert pages[0]["widget"] == "webview"
    assert pages[0]["props"]["pluginId"] == "snippet_box"
    endpoint = manifest["http_endpoints"][0]
    assert endpoint["method"] == "GET"
    assert endpoint["path"] == "/ext/snippet_box/page/snippet_box"
    assert endpoint["auth"] == "user"
    assert endpoint["handler_capability"] == "http.handle"


def test_handle_returns_html_envelope() -> None:
    pytest.importorskip("agentos_plugin_sdk")
    server = _load_server()
    resp = server.handle(path="/ext/snippet_box/page/snippet_box", method="GET")
    assert resp["status"] == 200
    assert "text/html" in resp["headers"]["Content-Type"]
    assert resp["body_encoding"] == "base64"
    assert server.handle(path="/ext/snippet_box/page/other", method="GET")["status"] == 404
    assert server.handle(path="/ext/snippet_box/page/snippet_box", method="POST")["status"] == 404


def test_handle_inlines_data_snapshot(tmp_path, monkeypatch) -> None:
    pytest.importorskip("agentos_plugin_sdk")
    monkeypatch.setattr(impl, "DATA_PATH", _db(tmp_path))
    impl.snippet_box(action="add", title="内联验证片段", tags="demo", content="payload-check")
    server = _load_server()
    resp = server.handle(path="/ext/snippet_box/page/snippet_box", method="GET")
    body = base64.b64decode(resp["body"]).decode("utf-8")
    assert "内联验证片段" in body
    assert "payload-check" in body
    assert "__SNIPPET_DATA_JSON__" not in body  # 数据锚点必须被替换为真实快照
