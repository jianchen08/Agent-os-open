#!/usr/bin/env python3
"""MCP Bridge 测试替身——真 JSON-RPC stdio server（newline 分帧）。

行为：
- initialize → protocolVersion 回显 + serverInfo
- tools/list  → 两个假工具
- tools/call(name=stub)  → 回显 arguments.url 为 text content
- tools/call(name=boom)  → 返回 JSON-RPC error（供上游错误路径测试）
每读一行处理一行；stdout 无关消息（通知）直接忽略不回包。
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:
            continue  # notification / log —— 不回包
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub-upstream", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": "browser_navigate", "description": "stub navigate"},
                    {"name": "browser_snapshot", "description": "stub snapshot"},
                ]
            }
        elif method == "tools/call":
            name = str(params.get("name", ""))
            args = params.get("arguments") or {}
            if name == "boom":
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {"code": -32000, "message": "boom requested"}}), flush=True)
                continue
            text = str(args.get("url", f"called:{name}"))
            result = {"content": [{"type": "text", "text": text}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": f"unknown method {method}"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()