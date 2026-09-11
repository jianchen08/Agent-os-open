# @feature: FP-0.2.二 可观测性 | @ci: python-coverage
"""triggers_ext HTTP 面 wire 形状锁（http_json 收敛替换的契约锚）。

锁公开面 http.handle 的扁平错误信封（本地 `_error(message, status)`，与
http_json.error 同条契约）：``{"success": False, "error": <message>,
"data": {status, body: {"error": <message>}}}`` —— message 双写（外层信封 +
内层 body）。端点行为语义由 test_triggers_http.py 覆盖，本文件只锁错误
响应 wire 形状（含 400 body 解码失败路径与 404 fail-closed）。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = _REPO / "plugins" / "shared" / "tools" / "triggers_ext"
for _d in (str(_PLUGIN_DIR),):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import http_api  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单测：进程内单例，零外部依赖


def _call(
    path: str, method: str = "GET", raw_body: str = "", headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    import asyncio

    return asyncio.run(http_api.handle_http_dispatch(path, method, raw_body, {}, headers))


def _assert_flat_error(
    result: dict[str, Any], status: int, message: str, *, prefix: bool = False
) -> None:
    """锁扁平错误信封：success=False + message 双写 + data 内嵌 HTTP 状态。"""
    assert result["success"] is False, result
    if prefix:
        assert result["error"].startswith(message), result
    else:
        assert result["error"] == message  # 外层信封 message 原文
    data = result["data"]
    assert set(data.keys()) == {"status", "headers", "body", "body_encoding"}
    assert data["status"] == status
    assert data["body_encoding"] == "base64"
    assert data["headers"] == {"Content-Type": "application/json; charset=utf-8"}
    # 内层 body 同 message（双写契约，前端两条消费面各取其一）
    if prefix:
        assert json.loads(base64.b64decode(data["body"]).decode("utf-8"))["error"].startswith(
            message
        )
    else:
        assert json.loads(base64.b64decode(data["body"]).decode("utf-8")) == {"error": message}


def test_error_shape_404_unknown_path() -> None:
    _assert_flat_error(_call("/ext/trigger_setup_tool/nope"), 404,
                       "not found: GET /ext/trigger_setup_tool/nope")


def test_error_shape_404_unknown_method() -> None:
    _assert_flat_error(_call("/ext/trigger_setup_tool/triggers", "DELETE"), 404,
                       "not found: DELETE /ext/trigger_setup_tool/triggers")


def test_error_shape_400_invalid_body() -> None:
    _assert_flat_error(
        _call("/ext/trigger_setup_tool/triggers", "POST", raw_body="not-json"),
        400,
        "invalid JSON body",
        prefix=True,
    )
