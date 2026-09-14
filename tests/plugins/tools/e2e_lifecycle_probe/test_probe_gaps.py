# @feature: FP-0.2.一 插件协议 | @ci: python-coverage
"""e2e_lifecycle_probe server.py 缺口补测（第 88 行 ValueError 分支）。

`_decode_raw_body` 对"JSON 合法但非 object"的 body 抛
``ValueError("body must be a JSON object")``，由 http_handle 归一为 400。
两种传输形态各测：内核 base64 字节透传形态与明文 JSON 宽容形态。
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
_ECHO_PATH = "/ext/e2e_lifecycle_probe/echo"


def _load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "e2e_probe_server_gaps_under_test", _PLUGIN_DIR / "server.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def server() -> Any:
    return _load_server_module()


def _decode_body(response: dict[str, Any]) -> Any:
    return json.loads(base64.b64decode(response["body"]).decode("utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[1, 2], None, "str"])
@pytest.mark.parametrize("body_form", ["base64", "plain"])
async def test_non_object_json_body_rejected_400(
    server: Any, payload: Any, body_form: str
) -> None:
    """JSON 合法但非 dict（数组/null/字符串）→ 400 + 契约文案。"""
    text = json.dumps(payload)
    raw_body = base64.b64encode(text.encode("utf-8")).decode("ascii") if body_form == "base64" else text

    out = await server.http_handle(path=_ECHO_PATH, method="POST", raw_body=raw_body)

    assert out["status"] == 400
    assert _decode_body(out)["error"] == "body must be a JSON object"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"message": "hi"}, {"extra": 1}])
async def test_object_json_still_accepted_200(server: Any, payload: dict) -> None:
    """对照组：dict 形态照常 200（边界只针对非 object）。"""
    raw_body = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    out = await server.http_handle(path=_ECHO_PATH, method="POST", raw_body=raw_body)

    assert out["status"] == 200
    assert _decode_body(out)["alive"] is True
