#!/usr/bin/env python3
"""测试用 fake ACP agent——手搓线缆忠实实现（ndjson JSON-RPC over stdio，零第三方依赖）。

被 test_acp_dispatch.py 以子进程拉起（sys.executable），对引擎的 SDK 客户端
回真实 ACP 线缆握手/回合。宽容语义对齐生产 peer（opencode/TS，zod 填缺省）：
content block 不强校验判别字段。行为变体经 argv 模式开关（default=echo）：

- echo（缺省）：把 prompt 文本回传（agent_message_chunk），stopReason=end_turn；
- fail-init：initialize 回 -32000 错误（协议错误面）；
- quota：session/prompt 回含额度措辞的 -32000（撞墙归类断言语料）；
- slow：prompt 延迟 N 秒（argv[2]，缺省 5；超时归类断言）；
- refuse：prompt 正常返回但 stopReason=refusal（非 end_turn 收口）；
- perm：prompt 先向客户端发 session/request_permission（options 含
  allow_once/reject_once），把客户端所选 optionId 回显进回复文本
  （客户端许可策略的行为断言面）。

诊断一律走 stderr，stdout 为 ACP 协议专线不得混入；IO 走字节流（UTF-8，
不沾控制台代码页）。
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any


def _mode() -> str:
    return sys.argv[1] if len(sys.argv) > 1 else "echo"


def _prompt_delay_seconds() -> float:
    if _mode() != "slow":
        return 0.0
    return float(sys.argv[2]) if len(sys.argv) > 2 else 5.0


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _result(msg_id: int, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id: int, message: str, code: int = -32000) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _notify_session_update(session_id: str, text: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
    )


def _prompt_text(params: dict[str, Any]) -> str:
    blocks = params.get("prompt", [])
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _ask_permission(session_id: str) -> str:
    """向客户端发 request_permission 并等应答，返回所选 optionId（无果 none）。"""
    request_id = 900000 + int(uuid.uuid4().hex[:6], 16)
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": "fake-tc-1",
                    "title": "fake",
                    "kind": "read",
                    "rawInput": {},
                },
                "options": [
                    {"optionId": "opt-allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "opt-reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            return "eof"
        message = json.loads(raw.decode("utf-8"))
        if message.get("id") == request_id and "result" in message:
            outcome = message["result"].get("outcome", {})
            return str(outcome.get("optionId", outcome.get("outcome", "none")))


def _handle_prompt(msg_id: int, params: dict[str, Any]) -> None:
    session_id = str(params.get("sessionId", ""))
    if _mode() == "quota":
        _error(msg_id, "usage limit reached: free tier quota exhausted (429)")
        return
    delay = _prompt_delay_seconds()
    if delay > 0:
        time.sleep(delay)
    reply = _prompt_text(params)
    if _mode() == "perm":
        reply = f"perm-selected={_ask_permission(session_id)} {reply}"
    _notify_session_update(session_id, reply)
    stop = "refusal" if _mode() == "refuse" else "end_turn"
    _result(msg_id, {"stopReason": stop})


def _handle(msg_id: int, method: str, params: dict[str, Any]) -> None:
    if method == "initialize":
        if _mode() == "fail-init":
            _error(msg_id, "fake agent refuses to initialize")
            return
        _result(msg_id, {"protocolVersion": 1, "agentCapabilities": {"promptCapabilities": {}}})
        return
    if method == "session/new":
        _result(msg_id, {"sessionId": f"fake-ses-{uuid.uuid4().hex[:12]}"})
        return
    if method == "session/load":
        _result(msg_id, {"sessionId": str(params.get("sessionId", ""))})
        return
    if method == "session/prompt":
        _handle_prompt(msg_id, params)
        return
    _error(msg_id, f"method not found: {method}", code=-32601)


def main() -> None:
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            print(f"fake agent: undecodable line: {exc}", file=sys.stderr)
            continue
        if not isinstance(message, dict) or "id" not in message or "method" not in message:
            continue  # 通知/应答不是本 fake 的输入面
        params = message.get("params") or {}
        _handle(int(message["id"]), str(message["method"]), params if isinstance(params, dict) else {})


if __name__ == "__main__":
    main()
