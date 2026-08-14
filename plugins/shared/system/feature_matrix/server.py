#!/usr/bin/env python3
"""Feature Matrix Plugin — 全功能渲染验证插件。

覆盖所有 widget 类型 / space / slot / schema 表单 / detachable / webview。
仅供端到端渲染验证,无业务逻辑。

> 2026-08: webcomponent 演示页已迁移为 webview（WebComponentCardHost 废弃，
> 见 docs/tasks/task_plugin_frontend_customization.md 任务 4）。原 /component.js
> 端点与 Custom Element 示例一并移除。

端点:
  GET /ext/feature_matrix_plugin/webview    → webview widget 的 HTML
  GET /ext/feature_matrix_plugin/wc_demo    → 原 webcomponent 演示页的 webview 迁移版
  GET /ext/feature_matrix_plugin/config     → 插件配置数据(schema 表单的数据源)
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("feature_matrix_plugin")
logger = logging.getLogger(__name__)


def _response_html(html: str) -> dict[str, Any]:
    """构造 HTML 成功响应(内核 dispatcher 期望的结构)。"""
    return {
        "success": True,
        "data": {
            "status": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
            "body_encoding": "base64",
        },
    }


def _response_json(data: dict[str, Any]) -> dict[str, Any]:
    """构造 JSON 成功响应。"""
    body = json.dumps(data, ensure_ascii=False)
    return {
        "success": True,
        "data": {
            "status": 200,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            "body_encoding": "base64",
        },
    }


WEBVIEW_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: system-ui, sans-serif; padding: 16px; color: #e0e0e0; background: #0d1117; margin: 0; }
    h2 { color: #22D3EE; margin-top: 0; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; margin: 8px 0; }
    button { background: #238636; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; }
    button:hover { background: #2ea043; }
    #log { margin-top: 12px; font-size: 12px; color: #8b949e; min-height: 30px; font-family: monospace; }
  </style>
</head>
<body>
  <h2>🌐 Feature Matrix Webview</h2>
  <p>这是插件提供的自由 UI(VS Code 风格沙箱 iframe)。</p>
  <div class="card">
    <strong>验证项:</strong>
    <ul>
      <li>iframe srcDoc 渲染 ✅</li>
      <li>sandbox 隔离(opaque origin)</li>
      <li>postMessage 上行(点按钮测试)</li>
    </ul>
  </div>
  <button onclick="pingHost()">调用宿主 postMessage</button>
  <div id="log">等待操作...</div>
  <script>
    function pingHost() {
      if (window.agentos) {
        var id = window.agentos.postMessage('fm.hello', { msg: '来自 webview 的问候', ts: Date.now() });
        document.getElementById('log').innerText = '上行已发送: ' + id + ' (method: fm.hello)';
      } else {
        document.getElementById('log').innerText = 'window.agentos 不可用';
      }
    }
    window.addEventListener('message', function(e) {
      var d = e.data;
      if (d && d.__agentos_webview && d.method) {
        document.getElementById('log').innerText = '收到下行: ' + d.method + ' => ' + JSON.stringify(d.params || {}).slice(0, 100);
      }
    });
  </script>
</body>
</html>"""


WEBVIEW_WC_DEMO_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: system-ui, sans-serif; padding: 16px; color: #e0e0e0; background: #0d1117; margin: 0; }
    .wc-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
               color: white; border-radius: 12px; padding: 20px; margin: 8px 0;
               box-shadow: 0 4px 12px rgba(102,126,234,0.3); }
    h3 { margin: 0 0 8px 0; }
    p { margin: 4px 0; opacity: 0.9; }
    .badge { display: inline-block; background: rgba(255,255,255,0.2);
             padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-top: 8px; }
  </style>
</head>
<body>
  <div class="wc-card">
    <h3>🧩 原 Web Component 演示（已迁移 Webview）</h3>
    <p>Hello from Webview (was: Custom Element demo)!</p>
    <p>iframe 沙箱隔离,postMessage 通信——替代废弃的 new Function 注入路径。</p>
    <span class="badge">WebviewWidget + sandbox</span>
  </div>
</body>
</html>"""


CONFIG_DATA = {
    "api_endpoint": "https://api.feature-matrix.local",
    "timeout": 45,
    "enabled": True,
    "log_level": "debug",
    "tags": ["core", "exp"],
    "description": "Feature Matrix 测试插件的示例配置数据(由后端 GET /config 返回)"
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
    """处理 3 个端点:webview HTML / wc_demo(迁移版) / config JSON。"""
    if path.endswith("/webview"):
        return _response_html(WEBVIEW_HTML)
    if path.endswith("/wc_demo"):
        return _response_html(WEBVIEW_WC_DEMO_HTML)
    if path.endswith("/config"):
        return _response_json(CONFIG_DATA)
    # 未知路径
    return {
        "success": True,
        "data": {
            "status": 404,
            "headers": {"Content-Type": "application/json"},
            "body": base64.b64encode(b'{"error":"not found"}').decode("ascii"),
            "body_encoding": "base64",
        },
    }


if __name__ == "__main__":
    plugin.run()
