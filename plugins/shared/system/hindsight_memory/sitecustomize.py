"""hindsight-api 子进程专用 mcp 兼容 shim（经子进程 PYTHONPATH 生效）。

背景：AgentOS 插件 SDK 锁 mcp>=2.0,<3（`MCPError`、2.0 API 面），而
hindsight-api-slim 的 extensions 顶层无条件 import fastmcp server，
fastmcp 3.4 按 mcp 1.x API 写（`request_ctx` ContextVar、`McpError`）。
两个生态在同 site-packages 下互斥。

本文件由 on_load 启动 hindsight-api 子进程时注入 PYTHONPATH 前置，
python 启动时自动执行，做最小兼容注入：

1. `mcp.server.lowlevel.server.request_ctx`：mcp 2.0 移除的 ContextVar。
   fastmcp 仅 `request_ctx.get()` 读取（无值时经其 HTTP context fallback，
   见 fastmcp/server/dependencies.py），注入 default=None 的 ContextVar 即可。
   AgentOS 只消费 hindsight 的 HTTP REST 面（aretain/arecall），MCP 面永不
   被调用，该 var 永远无值，不影响任何实际行为。
2. `mcp.shared.exceptions.MCPError = McpError`：1.x/2.0 大小写别名。
   fastmcp import 面里引用的异常名补齐。

主环境（内核 SDK 侧）不受影响——shim 只在子进程 PYTHONPATH 里可见。
若 fastmcp 后续版本出现更多 1.x-only API，此处按需追加；hindsight-all-slim
发布 mcp 2.0 兼容版后本文件可整体删除。
"""

from __future__ import annotations

import contextvars

try:  # mcp 未安装时静默（hindsight 自身的懒导入会给出降级路径）
    import mcp.server.lowlevel.server as _lowlevel_server
    import mcp.shared.exceptions as _mcp_exc

    if not hasattr(_lowlevel_server, "request_ctx"):
        _lowlevel_server.request_ctx = contextvars.ContextVar(
            "request_ctx", default=None
        )
    if not hasattr(_mcp_exc, "MCPError") and hasattr(_mcp_exc, "McpError"):
        _mcp_exc.MCPError = _mcp_exc.McpError
except Exception:  # noqa: BLE001 - sitecustomize 永不让解释器启动失败
    pass
