#!/usr/bin/env python3
"""Mock OpenAI 兼容 LLM 服务器 —— 用于端到端工具调用链路验证。

行为：
- POST /v1/chat/completions
- 第一轮（messages 无 role=tool）：返回 tool_calls 调用 scientific_calculator("5+3")
- 后续轮（messages 含 role=tool）：返回纯文本最终答案 "5+3 的计算结果是 8。"
- 支持流式（SSE）与非流式两种响应

用途：在无真实 LLM API key 的环境下触发完整工具调用链路
（前端 → WS → 内核 → llm_core → mock LLM → tool_core → 工具执行 → WS 事件 → 前端渲染）。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 18080

TOOL_NAME = "scientific_calculator"
TOOL_ARGS = {"operation": "calculate", "expression": "5+3"}
FINAL_TEXT = "5+3 的计算结果是 8。"


def _has_tool_result(messages: list) -> bool:
    return any(m.get("role") == "tool" for m in messages)


def _build_tool_calls_response() -> dict:
    tool_calls = [{
        "id": "call_mock_001",
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "arguments": json.dumps(TOOL_ARGS, ensure_ascii=False),
        },
    }]
    return {
        "id": "chatcmpl-mock-001",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-v4-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


def _build_text_response() -> dict:
    return {
        "id": "chatcmpl-mock-002",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-v4-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": FINAL_TEXT},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except Exception:
            req = {}
        messages = req.get("messages", [])
        stream = req.get("stream", False)
        has_tool_result = _has_tool_result(messages)

        if stream:
            self._send_sse(has_tool_result)
        else:
            resp = _build_text_response() if has_tool_result else _build_tool_calls_response()
            data = json.dumps(resp, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _send_sse(self, has_tool_result: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if has_tool_result:
            chunks = [
                {"id": "chatcmpl-mock-002", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": "chatcmpl-mock-002", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": FINAL_TEXT}, "finish_reason": None}]},
                {"id": "chatcmpl-mock-002", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
        else:
            arg = json.dumps(TOOL_ARGS, ensure_ascii=False)
            chunks = [
                {"id": "chatcmpl-mock-001", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}, "finish_reason": None}]},
                {"id": "chatcmpl-mock-001", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_mock_001", "type": "function",
                                                                    "function": {"name": TOOL_NAME, "arguments": ""}}]},
                              "finish_reason": None}]},
                {"id": "chatcmpl-mock-001", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": arg}}]},
                              "finish_reason": None}]},
                {"id": "chatcmpl-mock-001", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        for c in chunks:
            line = "data: " + json.dumps(c, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Mock LLM server listening on http://127.0.0.1:{PORT}/v1/chat/completions")
    server.serve_forever()
