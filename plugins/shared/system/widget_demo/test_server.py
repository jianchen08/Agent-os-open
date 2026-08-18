"""@file widget_demo server 端点测试（http.handle 分发）。

直接调 http_handle（path/method/query/raw_body），断言 envelope 解码后的
响应体——与前端 FormWidget datasourceUri/dataUri/endpoint 声明对接的形状。
"""

import asyncio
import base64
import json

import pytest


@pytest.fixture(autouse=True)
def _reset_counter():
    from server import _state

    _state.update({"fetch_count": 0, "submit_count": 0, "toggle_count": 0})
    yield


def _call(path, method="GET", query=None, raw_body=""):
    from server import http_handle

    return asyncio.run(
        http_handle(path=path, method=method, plugin_id="widget_demo", query=query or {}, raw_body=raw_body)
    )


def _decode(data):
    assert data.get("status") == 200, data
    body = base64.b64decode(data["body"]).decode("utf-8")
    return json.loads(body)


def _body_b64(obj):
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def test_config_get_increments_fetch_count():
    out = _call("/ext/widget_demo/config")
    assert out["success"] is True
    cfg = _decode(out["data"])
    assert cfg["fetch_count"] == 1
    assert "enabled" in cfg and "threshold" in cfg
    # 再取一次递增（G6-b poll 可见变化）
    out2 = _call("/ext/widget_demo/config")
    assert _decode(out2["data"])["fetch_count"] == 2


def test_schema_returns_fields():
    out = _call("/ext/widget_demo/schema")
    fields = _decode(out["data"])["fields"]
    names = [f["name"] for f in fields]
    assert names == ["enabled", "threshold", "note"]


def test_state_submit_count():
    out = _call("/ext/widget_demo/state")
    assert _decode(out["data"])["submit_count"] == 0


def test_options_models_by_provider():
    # G2 级联：模板携带 provider=zhipu
    out = _call("/ext/widget_demo/options/models", query={"provider": "zhipu"})
    opts = _decode(out["data"])["options"]
    assert [o["value"] for o in opts] == ["glm-5.2", "glm-4.7"]
    # 未知 provider 兜底
    out2 = _call("/ext/widget_demo/options/models", query={"provider": "nope"})
    assert _decode(out2["data"])["options"][0]["value"] == "unknown-model"


def test_options_regions():
    out = _call("/ext/widget_demo/options/regions")
    assert len(_decode(out["data"])["options"]) == 3


def test_submit_increments_and_echoes():
    body = _body_b64({"title": "T1", "pipeline_id": "p-1"})
    out = _call("/ext/widget_demo/actions/submit", method="POST", raw_body=body)
    data = _decode(out["data"])
    assert data["ok"] is True
    assert data["echo"]["title"] == "T1"
    assert data["submit_count"] == 1
    # state 反映增长（G3 watch 订阅侧可观察到）
    assert _decode(_call("/ext/widget_demo/state")["data"])["submit_count"] == 1


def test_toggle_returns_switched():
    out = _call("/ext/widget_demo/actions/toggle", method="POST", raw_body=_body_b64({"mode": "strict"}))
    data = _decode(out["data"])
    assert data["switched"] is True
    assert data["mode"] == "strict"


def test_unknown_route_404():
    out = _call("/ext/widget_demo/nope")
    assert out["success"] is True
    assert out["data"]["status"] == 404
