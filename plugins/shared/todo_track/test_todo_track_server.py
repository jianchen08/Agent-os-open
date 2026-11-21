# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage

"""todo_track server.py 适配层测试——步骤注册一致性 / 降级契约 / 端点分发。"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent

for _d in [str(_DIR)]:
    if str(_d) not in sys.path:
        sys.path.insert(0, _d)


def _load_server() -> Any:
    """按显式路径加载 server.py（裸名 server 全车道共跑会被劫持）。

    先逐出陈旧裸名 `plugin`：server.py 顶层 `from plugin import TodoTrackPlugin`
    走 sys.modules 缓存——共跑车道里其他插件的测试会把自身 plugin.py 装进
    裸名缓存，不逐出则劫持到错误实现上 ImportError。
    """
    sys.modules.pop("plugin", None)
    mod_name = "todo_track_server_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "server.py"))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_declared_steps_registered() -> None:
    srv = _load_server()
    assert sorted(srv.plugin.steps) == ["todo.extract", "todo.fold"]


def test_declared_steps_match_manifest() -> None:
    srv = _load_server()
    manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = sorted(s["name"] for s in manifest["capabilities"]["steps"])
    assert declared == sorted(srv.plugin.steps)


async def test_todo_fold_handler_returns_state_updates() -> None:
    # 缓存契约（2026-10-23）：fold 产出并入 prompt.dynamic_vars，系统提示词零触碰
    srv = _load_server()
    result = await srv.todo_fold(
        {"context.system_prompt": "S", "prompt.dynamic_vars": {"role": "user", "content": "基座"}},
        None,
    )
    assert "state_updates" in result
    updates = result["state_updates"]
    assert "context.system_prompt" not in updates
    assert "prompt.dynamic_vars" in updates
    from plugin import TODO_INSTRUCTIONS

    assert TODO_INSTRUCTIONS in updates["prompt.dynamic_vars"]["content"]


async def test_todo_extract_handler_degrades_on_error(monkeypatch: Any) -> None:
    # 降级契约：插件层异常 → warning + 空 state_updates 直通，不阻断管道
    class Boom:
        async def extract(self, _state: dict) -> dict:
            raise RuntimeError("boom")

    srv = _load_server()

    def _boom_get_instance() -> Boom:
        return Boom()

    monkeypatch.setattr(srv, "get_instance", _boom_get_instance)
    assert await srv.todo_extract({"messages": []}, None) == {"state_updates": {}}


async def test_todo_fold_handler_degrades_on_error(monkeypatch: Any) -> None:
    class Boom:
        async def fold(self, _state: dict) -> dict:
            raise RuntimeError("boom")

    srv = _load_server()

    def _boom_get_instance() -> Boom:
        return Boom()

    monkeypatch.setattr(srv, "get_instance", _boom_get_instance)
    assert await srv.todo_fold({}, None) == {"state_updates": {}}


def test_http_handle_latest_with_payload() -> None:
    srv = _load_server()
    srv.get_instance()._latest["pipe-x"] = {
        "items": [{"text": "任务", "done": True}],
        "updated_at": "2026-10-08T00:00:00+00:00",
    }
    resp = _run(
        srv.http_handle(
            path="/ext/pipeline_todo_track/latest",
            method="GET",
            query={"pipeline_id": "pipe-x"},
        )
    )
    assert resp["success"] is True
    data = resp["data"]
    assert data["status"] == 200
    body = json.loads(base64.b64decode(data["body"]))
    assert body["todo"]["items"][0]["text"] == "任务"


def test_http_handle_latest_without_pipeline_id_empty_payload() -> None:
    srv = _load_server()
    resp = _run(
        srv.http_handle(path="/ext/pipeline_todo_track/latest", method="GET")
    )
    body = json.loads(base64.b64decode(resp["data"]["body"]))
    assert body["todo"] is None


def test_http_handle_latest_without_pipeline_id_returns_most_recent() -> None:
    # 无参形态 = 最近更新载荷（webview 计划页免 session→pipeline 映射）
    srv = _load_server()
    inst = srv.get_instance()
    inst._latest["pipe-old"] = {
        "items": [{"text": "旧", "done": False}],
        "updated_at": "2026-10-08T01:00:00+00:00",
    }
    inst._latest["pipe-new"] = {
        "items": [{"text": "新", "done": True}],
        "updated_at": "2026-10-08T02:00:00+00:00",
    }
    resp = _run(
        srv.http_handle(path="/ext/pipeline_todo_track/latest", method="GET")
    )
    body = json.loads(base64.b64decode(resp["data"]["body"]))
    assert body["todo"]["items"][0]["text"] == "新"


def test_http_handle_latest_by_id_returns_that_bucket_not_most_recent() -> None:
    # 显式 pipeline_id = 精确取桶（角标契约，2026-10-27 用户裁定）：后台其他
    # 管道最近更新不得劫持当前会话角标
    srv = _load_server()
    inst = srv.get_instance()
    inst._latest["pipe-idle"] = {
        "items": [{"text": "当前任务待办", "done": False}],
        "updated_at": "2026-10-27T01:00:00+00:00",
    }
    inst._latest["pipe-bg"] = {
        "items": [{"text": "后台任务待办", "done": False}],
        "updated_at": "2026-10-27T05:00:00+00:00",
    }
    resp = _run(
        srv.http_handle(
            path="/ext/pipeline_todo_track/latest",
            method="GET",
            query={"pipeline_id": "pipe-idle"},
        )
    )
    body = json.loads(base64.b64decode(resp["data"]["body"]))
    assert body["todo"]["items"][0]["text"] == "当前任务待办"


def test_http_handle_page_serves_html() -> None:
    srv = _load_server()
    resp = _run(
        srv.http_handle(
            path="/ext/pipeline_todo_track/page/todo-plan", method="GET"
        )
    )
    assert resp["success"] is True
    data = resp["data"]
    assert data["status"] == 200
    assert "text/html" in data["headers"]["Content-Type"]
    html = base64.b64decode(data["body"]).decode("utf-8")
    assert "任务计划" in html
    assert "/ext/pipeline_todo_track/latest" in html


def test_http_handle_unknown_path_404() -> None:
    srv = _load_server()
    resp = _run(
        srv.http_handle(path="/ext/pipeline_todo_track/other", method="GET")
    )
    assert resp["success"] is False
    assert resp["data"]["status"] == 404


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
