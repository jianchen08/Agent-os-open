#!/usr/bin/env python3
"""todo_track pipeline plugin MCP 服务端——纯接口适配层。

业务逻辑在 plugin.py；本文件只做接口适配：capabilities.steps 双步骤
（todo.fold / todo.extract）具名注册 + latest 端点 http.handle 分发。
内核经 _step_method 具名调用（接线 ADR 2026-10-08-step-service-level3-wiring）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录（本地 plugin.py）+ plugins/shared 根入 sys.path

from plugin import TodoTrackPlugin  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("pipeline_todo_track")

_PLAN_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "todo_plan.html"
)


def _plan_html_response() -> dict[str, Any]:
    """包内 webview 计划页 → HttpHandleResponse（text/html，body base64）。

    面板页供给契约与各模式包 webview 页同构：页面路由经 http_endpoints
    （handler_capability=http.handle），HTML 单文件、body base64。"""
    with open(_PLAN_HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize todo_track plugin."""
    get_instance()


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup todo_track plugin."""
    global _instance
    _instance = None


_instance: TodoTrackPlugin | None = None


def get_instance() -> TodoTrackPlugin:
    """懒构建并缓存插件单例。"""
    global _instance
    if _instance is None:
        _instance = TodoTrackPlugin(config=plugin.get_config())
    return _instance


@plugin.step("todo.fold")
async def todo_fold(state: dict, config: dict | None = None) -> dict:
    """输入阶段：说明常量追加系统提示词 + 当前清单追加动态变量消息尾部。"""
    try:
        return {"state_updates": await get_instance().fold(state or {})}
    except Exception as exc:  # noqa: BLE001 — 降级契约：失败不阻断管道
        logger.warning("[todo_fold] 执行失败，零写入直通 | err=%s", exc)
        return {"state_updates": {}}


@plugin.step("todo.extract")
async def todo_extract(state: dict, config: dict | None = None) -> dict:
    """输出阶段：提取最后完整清单块，写 context.todo_text + latest 载荷。"""
    try:
        return {"state_updates": await get_instance().extract(state or {})}
    except Exception as exc:  # noqa: BLE001 — 降级契约：失败不阻断管道
        logger.warning("[todo_extract] 执行失败，零写入直通 | err=%s", exc)
        return {"state_updates": {}}


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
    description="HTTP endpoint handler for /ext/pipeline_todo_track/** (latest todo payload)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict:
    """按 path 分发到 todo_track 端点（latest 载荷 + webview 计划页）。"""
    q = query or {}
    if path == "/ext/pipeline_todo_track/latest" and method == "GET":
        pipeline_id = q.get("pipeline_id", "")
        # 带 pipeline_id = 按管道取；缺省 = 最近更新载荷（webview 计划页与
        # 角标免 session→pipeline 映射的取数形态——最近更新者即当前活动管道）
        payload = (
            get_instance().latest_payload(pipeline_id)
            if pipeline_id
            else get_instance().latest_any()
        )
        body = json.dumps({"todo": payload}, ensure_ascii=False)
        return {
            "success": True,
            "data": {
                "status": 200,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                "body_encoding": "base64",
            },
        }
    if path == "/ext/pipeline_todo_track/page/todo-plan" and method == "GET":
        return {"success": True, "data": _plan_html_response()}
    return {
        "success": False,
        "error": f"未知端点: {path}",
        "data": {
            "status": 404,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": base64.b64encode(b'{"error": "not found"}').decode("ascii"),
            "body_encoding": "base64",
        },
    }


if __name__ == "__main__":
    plugin.run()
