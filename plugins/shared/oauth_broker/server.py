#!/usr/bin/env python3
"""OAuth Broker 插件 MCP 服务端——工具注册 + 本机回调路由（接口适配层）。

- ``oauth``（authorize / status 二合一，工具面退役任务C刀3）：声明面单源 =
  ``plugin.json`` ``capabilities.tools``（tools/list 上报与声明逐字一致）；
- ``http.handle``：内核 ``/ext/oauth_broker/**`` 分发面，只服务 ``GET CALLBACK_PATH``
  （授权服务器把浏览器重定向到这里；``auth=none`` 是声明面事实，凭 ``state`` +
  ``iss`` + PKCE 三道闸门把关）。

回调响应是浏览器面 HTML（非 JSON）：成功 200 提示可关闭窗口，失败 400 给可操作
提示；凭据 / verifier / code 一律不回显。

合宿契约：server.py 暴露模块级 plugin 实例（host.py getattr(module, "plugin")），
注册在装载期完成。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

import flow  # noqa: E402
from _libs.tool_server_base import load_tool_class, tool_result_envelope  # noqa: E402
from _root_api import json_response, ok  # noqa: E402

plugin = AgentOSPlugin("oauth_broker")

_impl_cls: dict[str, Any] = {}


def _manifest_tools() -> dict[str, dict[str, Any]]:
    """读 plugin.json 的工具声明（input_schema/description 单源）。

    「插件即声明」：plugin.json 是唯一书写处，tools/list 上报必须与声明逐字一致
    ——手抄 schema 与声明面漂移即被内核 G2 净化全量剔除。读失败让异常传播
    （sidecar 起不来 → G2 观测失败 ≠ 判定失败，保留声明注册待复验），不做静默回退。
    """
    manifest = json.loads((Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in manifest["capabilities"]["tools"]}


_MANIFEST_TOOLS = _manifest_tools()


def _load_tool_class(class_name: str) -> Any:
    """显式按文件路径加载本目录 tool.py 的工具类（合宿防遮蔽），按类名缓存。"""
    cached = _impl_cls.get(class_name)
    if cached is not None:
        return cached
    cls = load_tool_class(os.path.dirname(__file__), "oauth_broker_tool_impl", class_name)
    _impl_cls[class_name] = cls
    return cls


_STATUS_ONLY_PARAMS = ("client_id", "scope", "wait_seconds", "callback_base_url",
                       "client_secret", "resource_metadata_url", "guidance_action")


@plugin.tool(
    name="oauth",
    schema=_MANIFEST_TOOLS["oauth"]["input_schema"],
    description=_MANIFEST_TOOLS["oauth"]["description"],
)
async def oauth(action: str = "", **kwargs: Any) -> dict[str, Any]:
    """oauth 操作面（authorize / status 二合一，工具面退役任务C刀3）。

    按 action 分发到既有实现体（OAuthAuthorizeTool / OAuthStatusTool 原地复用）；
    跨动作参数运行时教学错误（错误教改法）。
    """
    if action == "authorize":
        result = await _load_tool_class("OAuthAuthorizeTool")(
            guidance=(plugin.get_config() or {}).get("guidance"),
        ).execute(kwargs)
        return tool_result_envelope(result)
    if action == "status":
        foreign = [k for k in _STATUS_ONLY_PARAMS if k in kwargs]
        if foreign:
            return {"success": False, "error_code": "OPTION_INVALID",
                    "error": (f"{', '.join(foreign)} 仅 action='authorize' 可用；"
                              "status 只需 action（server_url 可选过滤单服务器），"
                              "示例: oauth(action='status') 或 "
                              "oauth(action='status', server_url='<服务器URL>')")}
        result = await _load_tool_class("OAuthStatusTool")().execute(kwargs)
        return tool_result_envelope(result)
    return {"success": False, "error_code": "OPTION_INVALID",
            "error": (f"action 非法: {action!r}（允许: 'authorize' | 'status'）；"
                      "示例: oauth(action='authorize', server_url='<服务器URL>') 或 "
                      "oauth(action='status')")}


def _html_response(html: str, status: int) -> dict[str, Any]:
    """浏览器面响应（text/html；内核 HttpHandleResponse 契约：body base64）。"""
    return {
        "status": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


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
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """本机回调面：只认 ``GET CALLBACK_PATH``，其余 404/405。

    签名覆盖 HttpHandleRequest 全部字段——SDK 的 ``td.handler(**arguments)``
    会把内核传入的整个 request 对象展开为关键字参数。
    """
    del plugin_id, raw_body, headers
    if path != flow.CALLBACK_PATH:
        return ok(json_response({"error": "no route for oauth_broker callback face"}, 404))
    if method.upper() != "GET":
        return ok(_html_response(_plain_page("405", "回调只接受 GET"), 405))
    status, body = await flow.handle_callback(dict(query or {}))
    return ok(_html_response(body, status))


def _plain_page(title: str, message: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body><h1>{title}</h1><p>{message}</p></body></html>"
    )


if __name__ == "__main__":
    plugin.run()
