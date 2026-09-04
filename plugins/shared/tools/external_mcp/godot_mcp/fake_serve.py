# @feature: FP-0.2.〇 godot_run 测试假 serve | 仅供 test_godot_run.py 使用
"""假 godot-mcp-go serve——MCP stdio 假实现，支撑 godot_run 全链单测。

环境变量：
- FAKE_SPAWN_FILE：进程 spawn 追加记录（argv JSON 行）；退出后写 <file>.eof
- FAKE_CALLS_FILE：tools/call 次数追加记录（每次一行 "call"）
- FAKE_UNREACHABLE_FIRST=1：第 1 次 tools/call 返回 editor_unreachable（isError）
- FAKE_ALWAYS_UNREACHABLE=1：所有 tools/call 恒返回 editor_unreachable
异常不落进程：单消息处理失败记 FAKE_SPAWN_FILE.err 并回 JSON-RPC 内部错误。
"""

from __future__ import annotations

import json
import os
import sys
import traceback


def _log_err(context: str) -> None:
    logpath = os.environ.get("FAKE_SPAWN_FILE", "") + ".err"
    with open(logpath, "a", encoding="utf-8") as f:
        f.write("=== " + context + " ===\n" + traceback.format_exc())


def _handle(raw: str, project: str, unreachable_first: bool, always_unreachable: bool, calls_file: str) -> dict | None:
    msg = json.loads(raw)
    if "id" not in msg:
        return None
    method, rid = msg.get("method"), msg["id"]
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"tools": [{"name": "godot_run", "inputSchema": {}}]},
        }
    if method == "tools/call":
        nth = 0
        if calls_file:
            with open(calls_file, "a", encoding="utf-8") as f:
                f.write("call\n")
            with open(calls_file, encoding="utf-8") as f:
                nth = len(f.read().splitlines())
        a = (msg.get("params") or {}).get("arguments") or {}
        if (unreachable_first and nth == 1) or always_unreachable:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": "editor_unreachable: no editor running"}],
                    "isError": True,
                },
            }
        out = {"method": a.get("method"), "params": a.get("params"), "project": project}
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                "isError": False,
            },
        }
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}


def _run() -> None:
    spawn_file = os.environ.get("FAKE_SPAWN_FILE")
    if spawn_file:
        with open(spawn_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv[1:]) + "\n")
    args = sys.argv[1:]
    project = args[args.index("--project") + 1] if "--project" in args else ""
    unreachable_first = bool(os.environ.get("FAKE_UNREACHABLE_FIRST"))
    always_unreachable = bool(os.environ.get("FAKE_ALWAYS_UNREACHABLE"))
    calls_file = os.environ.get("FAKE_CALLS_FILE") or ""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            resp = _handle(raw, project, unreachable_first, always_unreachable, calls_file)
        except BaseException:
            _log_err("handling message")
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "fake crashed"}}
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    if spawn_file:
        with open(spawn_file + ".eof", "w", encoding="utf-8") as f:
            f.write("stdin-eof")


_run()
