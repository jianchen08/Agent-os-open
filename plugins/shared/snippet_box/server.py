#!/usr/bin/env python3
"""snippet_box 服务端（混合插件合宿）：LLM 工具 snippet_box + http.handle 供页。

http.handle 为运行面注册（plugin.json http_endpoints.handler_capability 指向），
不进 capabilities.tools（LLM 声明面）；snippet_box 为 LLM 声明面工具
（capabilities.tools[0] 与 snippet_box.py 常量逐字对应）。
合宿契约：server.py 必须暴露模块级 plugin 实例且装载期完成全部工具注册
（CohostServer 聚合 plugin._tools，注册留运行入口则合宿成员零工具）。
供页契约：读 data/snippets.json 快照，替换 page.html 内 __SNIPPET_DATA_JSON__
锚点内联（页面 CSP connect-src 'none' 禁出网，数据只能内联——mode_evolution 判例形态）。
"""

from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import snippet_box as impl

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("snippet_box")

_PAGE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")
_DATA_ANCHOR = "__SNIPPET_DATA_JSON__"

_HTTP_HANDLE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "method": {"type": "string"},
        "plugin_id": {"type": "string"},
        "raw_body": {"type": "string"},
        "headers": {"type": "object"},
        "query": {"type": "object"},
    },
}


def _inline_json(data: dict) -> str:
    """内联进 <script> 的 JSON 文本：转义 </script> 破坏面与 U+2028/2029 行终止符。"""
    text = json.dumps(data, ensure_ascii=False)
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def handle(path: str = "", method: str = "GET") -> dict:
    """serve 包内 page.html（dispatcher 契约：body base64 原样回写），快照内联。"""
    if method != "GET" or not path.endswith("/page/snippet_box"):
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    with open(_PAGE_HTML, "rb") as fh:
        body = fh.read().decode("utf-8")
    snapshot = {"snippets": impl._load_all(impl.DATA_PATH)}
    body = body.replace(_DATA_ANCHOR, _inline_json(snapshot))
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


def create_plugin() -> AgentOSPlugin:
    """注册 snippet_box（LLM 面）与 http.handle（供页运行面），幂等。"""
    plugin.register_tool(
        "snippet_box",
        impl.SNIPPET_BOX_INPUT_SCHEMA,
        impl.snippet_box,
        impl.SNIPPET_BOX_DESCRIPTION,
        output_schema=impl.SNIPPET_BOX_OUTPUT_SCHEMA,
        render=impl.SNIPPET_BOX_RENDER,
    )
    plugin.register_tool(
        "http.handle",
        _HTTP_HANDLE_SCHEMA,
        handle,
        "HTTP endpoint handler for /ext/snippet_box/page/snippet_box",
    )
    return plugin


create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


if __name__ == "__main__":
    run()
