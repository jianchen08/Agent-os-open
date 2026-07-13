"""
Demo MCP Server (stdio, JSON-RPC 2.0)

零依赖、纯标准库实现的 MCP 工具服务器，用于验证 MCP 客户端集成链路：
  1. initialize 握手
  2. tools/list 枚举
  3. tools/call 调用

提供工具：
  - echo(message) -> 原样回显
  - add(a, b)     -> 整数相加

协议约定（与 web-search-mcp 一致）：
  - 每行一个 JSON-RPC 请求/响应（以 \n 分隔）
  - 所有日志输出到 stderr，避免污染 stdout 的 JSON 流
"""

from __future__ import annotations

import json
import sys
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "demo-tools", "version": "1.0.0"}


def _write(obj: dict[str, Any]) -> None:
    """向 stdout 写一行 JSON。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    """日志写 stderr，不干扰 stdout 的 JSON 流。"""
    sys.stderr.write(f"[demo-tools] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 工具定义与实现
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "echo",
        "description": "原样回显输入消息（用于验证 MCP 调用链路）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "要回显的消息"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "返回两个整数的和",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "第一个加数"},
                "b": {"type": "integer", "description": "第二个加数"},
            },
            "required": ["a", "b"],
        },
    },
]


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        msg = arguments.get("message", "")
        return {"content": [{"type": "text", "text": str(msg)}]}
    if name == "add":
        a = int(arguments.get("a", 0))
        b = int(arguments.get("b", 0))
        return {"content": [{"type": "text", "text": str(a + b)}]}
    raise ValueError(f"未知工具: {name}")


# ---------------------------------------------------------------------------
# JSON-RPC 分发
# ---------------------------------------------------------------------------

def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理单个 JSON-RPC 请求，返回响应 dict（通知返回 None）。"""
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            }

        if method == "initialized" or method == "notifications/initialized":
            # 通知，无需响应
            return None

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            result = _call_tool(tool_name, arguments)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        # 未知方法
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    except Exception as e:  # noqa: BLE001
        _log(f"处理 {method} 失败: {e}")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": f"Internal error: {e}"},
        }


def main() -> None:
    """stdio 主循环：逐行读取 JSON-RPC 请求。"""
    _log(f"MCP demo-tools server 启动 (pid={__import__('os').getpid()})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"跳过非 JSON 行: {line[:80]} ({e})")
            continue
        response = handle(request)
        if response is not None:
            _write(response)


if __name__ == "__main__":
    main()
