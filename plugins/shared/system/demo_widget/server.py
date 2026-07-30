#!/usr/bin/env python3
"""Demo Widget Plugin MCP 服务端（v0.2 ADR §3.4/§3.5' 示范）。

演示三类前端集成链路的端到端验证：
1. contributes（statusBarItems/viewsContainers/widgets）— 纯声明，无运行时代码
2. metric_bindings 配置驱动推送 — 后台线程每秒 record_metric，内核 PluginWidgetBroadcaster 推前端
3. webview widget — http.handle 端点返回插件 HTML

本插件无业务逻辑，仅供联调验证。插件侧零 emit/push 调用（数据推送由内核统一编排）。

[来源: ADR §3.5' 内核统一配置驱动推送；§3.4' Webview widget]
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("demo_widget_plugin")

# 后台采样线程句柄
_stop_event: threading.Event | None = None
_sample_thread: threading.Thread | None = None
# 当前计数（供 webview 查询展示当前值）
_counter: int = 0


async def _sample_loop() -> None:
    """后台循环：每秒上报一次 demo.counter 指标（gauge 当前值）。

    用 gauge 而非 counter：状态栏/widget 显示的是「当前值」而非累计值，
    gauge 的 latest 直接反映最新状态，无需前端算差值。
    """
    global _counter
    loop = asyncio.get_running_loop()
    assert _stop_event is not None
    while not _stop_event.is_set():
        _counter += 1
        try:
            await plugin.record_metric(
                name="demo.counter",
                value=float(_counter),
                metric_type="gauge",
                unit="count",
                help_text="Demo widget plugin 每秒自增计数器",
            )
        except (KeyError, RuntimeError) as exc:
            # metrics 能力未注入或调用失败：记录但不中断采样
            # （内核可能未启用聚合器，或 sidecar 尚未完成 MCP 握手）
            print(f"[demo_widget_plugin] record_metric failed: {exc}", flush=True)
        # asyncio Event 不支持跨线程 is_set，用 threading.Event + run_in_executor 轮询
        await loop.run_in_executor(None, _stop_event.wait, 1.0)


@plugin.on_load
async def _on_load(_params: Any) -> None:
    """启动后台采样线程（独立事件循环，不阻塞 MCP 主循环）。"""
    global _stop_event, _sample_thread
    _stop_event = threading.Event()
    # 在独立线程跑采样循环（MCP 主循环在主线程，不能被阻塞）
    sample_loop_coro = _sample_loop()

    def _run_sample_loop() -> None:
        asyncio.run(sample_loop_coro)

    _sample_thread = threading.Thread(target=_run_sample_loop, daemon=True, name="demo-sample")
    _sample_thread.start()
    print("[demo_widget_plugin] on_load: 采样线程已启动", flush=True)


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
    """Webview widget 的 HTML 资源端点。

    前端 WebviewWidget fetch /ext/demo_widget_plugin/webview 时调用，
    返回简单 HTML（含调用 window.agentos 的演示）。

    签名覆盖 HttpHandleRequest 全部字段（method/path/plugin_id/raw_body/headers/query），
    因 SDK 的 ``td.handler(**arguments)`` 会把内核传入的整个 request 对象展开为关键字参数。
    """
    html = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: sans-serif; padding: 16px; color: #1a1a1a; }
    h2 { margin-top: 0; }
    .counter { font-size: 32px; font-weight: bold; color: #22D3EE; }
    button { padding: 8px 16px; margin: 4px; cursor: pointer; }
    #log { margin-top: 12px; font-size: 12px; color: #666; min-height: 40px; }
  </style>
</head>
<body>
  <h2>Demo Webview Widget</h2>
  <p>这是插件提供的自由 UI（VS Code 风格沙箱）。</p>
  <p>当前计数：<span class="counter" id="cnt">-</span></p>
  <div>
    <button onclick="ping()">调用宿主 (postMessage)</button>
  </div>
  <div id="log"></div>
  <script>
    // window.agentos 由宿主注入的 bootstrap JS 提供（postMessageSecurity）
    function ping() {
      if (window.agentos) {
        var id = window.agentos.postMessage('demo.ping', { ts: Date.now() });
        document.getElementById('log').innerText = '已发送上行消息: ' + id;
      } else {
        document.getElementById('log').innerText = 'window.agentos 不可用';
      }
    }
    // 接收宿主下行消息（widget.event）
    window.addEventListener('message', function(e) {
      var d = e.data;
      if (d && d.__agentos_webview && d.method === 'widget.event' && d.params) {
        var v = d.params.data && d.params.data.value;
        if (v !== undefined) {
          document.getElementById('cnt').innerText = v;
        }
      }
    });
  </script>
</body>
</html>"""
    # 内核 invoke_tool 对 sidecar 的 tool 调用，期望返回 ToolExecutionResult
    # 结构 {success, data, error}（见 invoker.rs:721）。http_dispatcher 再把
    # result.data 反序列化为 HttpHandleResponse，并对其 body 做 base64 解码
    # （dispatcher.rs:239 无条件 base64 decode）。故 body 须 base64 编码。
    return {
        "success": True,
        "data": {
            "status": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
            "body_encoding": "base64",
        },
    }


if __name__ == "__main__":
    plugin.run()
