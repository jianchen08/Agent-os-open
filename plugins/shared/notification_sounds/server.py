#!/usr/bin/env python3
"""notification_sounds 插件服务端——纯 HTTP 只读目录面。

单一路由 GET /ext/notification_sounds/sounds：返回合成预设全量（含渲染参数
与默认项标记）。sounds/*.json 即内容——用户/agent 落一个 JSON 文件即上目录，
每次请求现读盘（数据面天然热更，无需插件重载）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 sound_store）+ plugins/shared 根入 sys.path

from _root_api import (  # noqa: E402
    json_response as _json_response_body,
    ok as _ok,
)
from sound_store import load_sounds  # noqa: E402
from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("notification_sounds")

SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"


def _json_response(payload: dict, status: int = 200) -> dict:
    """ToolExecutionResult 信封（ok 包 HttpHandleResponse）。"""
    return _ok(_json_response_body(payload, status))


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
    description="Notification sounds catalog HTTP endpoint",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict:
    """提示音目录路由核。

    - GET /sounds：预设全量（按 id 升序）+ default_id（default:true 优先，
      无标记取 id 升序首个；目录为空时为空串，前端据此静默跳过提示音）。
    """
    if method == "GET" and path.endswith("/sounds"):
        sounds, default_id = load_sounds(SOUNDS_DIR)
        return _json_response({"sounds": sounds, "default_id": default_id})

    return _json_response({"error": f"unknown route {method} {path}"}, status=404)


if __name__ == "__main__":
    plugin.run()
