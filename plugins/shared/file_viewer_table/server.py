"""file_viewer_table 服务端（声明式文件查看器样例）

用途：证明「新增一类文件的查看器不改前端主代码」——本插件只做两件事：

1. `plugin.json` 的 `contributes.file_viewers` 声明 `.csv/.tsv` → 前端
   `fileViewerRegistry.loadFileViewers` 装载后，`resolveFileViewer('a.csv')`
   即路由到本插件（优先级 插件声明 > 内置表），前端零改动；
2. 提供一个自包含 HTML 页（`/ext/file_viewer_table/page/table_viewer`），
   作为 `render.widget='webview'` 的渲染件。

供页契约与 `snippet_box` 同款（合宿宿主 `http.handle` 注册，body base64 回写）。
文件坐标由宿主经 `widgetEventStore` 的 `file.open` 下行事件注入（立即推 + 1500ms
重推），页面侧在 `window.message` 里按 `__agentos_webview` 协议接收。内容取流归
插件页自理（webview 沙箱是 opaque origin，拿不到宿主 token，不能直读
`workspaces/file-content`）——本样例把路径展示与取流入口说明写清，不假装已取流。
"""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("file_viewer_table")

_PAGE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")

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


def handle(path: str = "", method: str = "GET") -> dict:
    """serve 包内 page.html（dispatcher 契约：body base64 原样回写）。"""
    if method != "GET" or not path.endswith("/page/table_viewer"):
        return {"status": 404, "headers": {}, "body": "", "body_encoding": "utf-8"}
    with open(_PAGE_HTML, "rb") as fh:  # noqa: PTH123
        body = fh.read().decode("utf-8")
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


def create_plugin() -> AgentOSPlugin:
    """注册 http.handle（供页运行面），幂等。"""
    plugin.register_tool(
        "http.handle",
        _HTTP_HANDLE_SCHEMA,
        handle,
        "HTTP endpoint handler for /ext/file_viewer_table/page/table_viewer",
    )
    return plugin


create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


if __name__ == "__main__":
    run()
