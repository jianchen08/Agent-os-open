# @feature: FP-0.2.一 插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""e2e_lifecycle_probe 探针插件单测——覆盖 server.py 全部分支。

探针是装卸载生命周期 e2e（tests/e2e_02/test_07_plugin_lifecycle_e2e.py）的
功能载体；此处直接调 async handler 验证回声工具与 http.handle 的协议契约
（200 回声 / 404 未知路径 / 400 非 JSON body / 明文 body 宽容解码）。
"""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tools" / "e2e_lifecycle_probe"
)


def _load_server_module():
    spec = importlib.util.spec_from_file_location(
        "e2e_probe_server_under_test", _PLUGIN_DIR / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def server():
    return _load_server_module()


def _b64(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


async def test_echo_tool_round_trip(server):
    """回声工具：message 原样返回 + 存活标记 + 插件 id。"""
    out = await server.e2e_probe_echo("hello-探针")
    assert out == {"plugin_id": "e2e_lifecycle_probe", "echo": "hello-探针", "alive": True}


async def test_http_handle_echo_ok(server):
    """http.handle 正路径：base64 body → 200 回声 JSON（body 再 base64）。"""
    out = await server.http_handle(
        path="/ext/e2e_lifecycle_probe/echo",
        method="POST",
        raw_body=_b64({"message": "via-ext"}),
    )
    assert out["status"] == 200
    assert out["body_encoding"] == "base64"
    decoded = json.loads(base64.b64decode(out["body"]).decode("utf-8"))
    assert decoded == {"plugin_id": "e2e_lifecycle_probe", "echo": "via-ext", "alive": True}


async def test_http_handle_unknown_path_404(server):
    """http.handle fail-closed：未知路径 404（与 triggers_ext 同款语义）。"""
    out = await server.http_handle(path="/ext/e2e_lifecycle_probe/other", method="GET")
    assert out["status"] == 404
    assert "not found" in base64.b64decode(out["body"]).decode("utf-8")


async def test_http_handle_bad_body_400(server):
    """http.handle：非 JSON object body → 400。"""
    out = await server.http_handle(
        path="/ext/e2e_lifecycle_probe/echo",
        method="POST",
        raw_body=base64.b64encode(b"not-json").decode("ascii"),
    )
    assert out["status"] == 400


async def test_http_handle_plain_json_body_tolerated(server):
    """http.handle 宽容分支：明文 JSON（非 base64）也解得开（triggers_ext 同款）。"""
    out = await server.http_handle(
        path="/ext/e2e_lifecycle_probe/echo",
        method="POST",
        raw_body=json.dumps({"message": "plain"}),
    )
    assert out["status"] == 200
    decoded = json.loads(base64.b64decode(out["body"]).decode("utf-8"))
    assert decoded["echo"] == "plain"


async def test_http_handle_empty_body_defaults(server):
    """http.handle：空 body → message 缺省空串，仍 200（探针无必填语义）。"""
    out = await server.http_handle(path="/ext/e2e_lifecycle_probe/echo", method="POST")
    assert out["status"] == 200
    decoded = json.loads(base64.b64decode(out["body"]).decode("utf-8"))
    assert decoded["echo"] == ""
