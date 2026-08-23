#!/usr/bin/env python3
"""E2E 生命周期探针插件——装卸载 e2e（tests/e2e_02/test_07_plugin_lifecycle_e2e.py）的功能载体。

刻意最小化：一个回声工具 + 一个 /ext HTTP 端点，无外部依赖、无副作用。
默认 disabled（config/plugins/default_profile.yaml），e2e 按需启停——
"功能生效/失效"的判据即本插件的工具与端点在内核面上的可见性与可调用性。
"""
from __future__ import annotations

import base64
import json
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

PLUGIN_ID = "e2e_lifecycle_probe"
ECHO_PATH = "/ext/e2e_lifecycle_probe/echo"

_ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "任意消息（将原样回声）"},
    },
    "required": ["message"],
}

plugin = AgentOSPlugin(PLUGIN_ID)


@plugin.tool(
    name="e2e_probe_echo",
    schema=_ECHO_SCHEMA,
    description="e2e 生命周期探针：原样回声 message 并附插件存活标记",
)
async def e2e_probe_echo(message: str = "") -> dict[str, Any]:
    """探针回声（无副作用）。"""
    return {"plugin_id": PLUGIN_ID, "echo": message, "alive": True}


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description=f"HTTP endpoint handler for POST {ECHO_PATH}",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /echo → 200 回声 JSON；其余路径 fail-closed 404。"""
    del method, plugin_id, headers, query  # 探针不消费这些透传字段
    if path != ECHO_PATH:
        return _http_response(404, json.dumps({"error": f"not found: {path}"}))
    try:
        body = _decode_raw_body(raw_body)
    except (json.JSONDecodeError, ValueError):
        return _http_response(400, json.dumps({"error": "body must be a JSON object"}))
    message = str(body.get("message", ""))
    return _http_response(
        200,
        json.dumps({"plugin_id": PLUGIN_ID, "echo": message, "alive": True}, ensure_ascii=False),
    )


def _decode_raw_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（内核铁律：base64 字节透传）为 dict。"""
    if not raw_body:
        return {}
    try:
        text = base64.b64decode(raw_body).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        text = raw_body  # 兼容明文 JSON（与 triggers_ext 同款宽容）
    parsed: Any = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("body must be a JSON object")
    return parsed


def _http_response(status: int, body: str) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    return {
        "status": status,
        "headers": {"content-type": "application/json"},
        "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


if __name__ == "__main__":
    plugin.run()
