# Lingxi AgentOS 0.2 — LLM litellm MCP 边车
#
# 通过 litellm 统一适配多 LLM provider，作为 MCP 服务端被 Rust 内核调用。
# 支持流式（SSE chunk → MCP notification）和非流式调用。
#
# 使用方式：
#   python3 litellm_proxy.py --model deepseek-chat
#
# [来源: docs/tasks/task_07_llm_api.md AC-06-2]

import argparse
import json
import sys
import os

def handle_request(request: dict) -> dict | None:
    """处理 MCP JSON-RPC 请求，返回响应 dict 或 None（notification 无响应）。"""
    method = request.get("method", "")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request.get("id", ""),
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": "agentos-llm-sidecar",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "tools": {},
                },
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request.get("id", ""),
            "result": {
                "tools": [
                    {
                        "name": "complete",
                        "description": "Non-streaming LLM completion",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "model": {"type": "string"},
                                "messages": {"type": "array"},
                                "temperature": {"type": "number"},
                                "max_tokens": {"type": "integer"},
                            },
                            "required": ["model", "messages"],
                        },
                    },
                    {
                        "name": "stream",
                        "description": "Streaming LLM completion (returns chunks via MCP notifications)",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "model": {"type": "string"},
                                "messages": {"type": "array"},
                                "temperature": {"type": "number"},
                                "max_tokens": {"type": "integer"},
                            },
                            "required": ["model", "messages"],
                        },
                    },
                    {
                        "name": "list_models",
                        "description": "List available LLM models",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            },
        }

    if method == "tools/call":
        args = request.get("params", {}).get("arguments", {})
        tool_name = request.get("params", {}).get("name", "")

        if tool_name == "list_models":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id", ""),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({
                                "models": [
                                    {"id": "deepseek-chat", "name": "DeepSeek Chat"},
                                    {"id": "gpt-4o", "name": "GPT-4o"},
                                    {"id": "claude-3-5-sonnet", "name": "Claude 3.5 Sonnet"},
                                ]
                            }),
                        }
                    ]
                },
            }

        if tool_name == "complete":
            try:
                import litellm
                response = litellm.completion(
                    model=args.get("model", "deepseek-chat"),
                    messages=args.get("messages", []),
                    temperature=args.get("temperature", 0.7),
                    max_tokens=args.get("max_tokens"),
                )
                choice = response.choices[0]
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id", ""),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({
                                    "content": choice.message.content or "",
                                    "tool_calls": None,
                                    "finish_reason": choice.finish_reason,
                                    "usage": {
                                        "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                                        "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                                        "total_tokens": getattr(response.usage, "total_tokens", 0),
                                    },
                                    "model": args.get("model", ""),
                                }),
                            }
                        ]
                    },
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id", ""),
                    "error": {"code": -32603, "message": str(e)},
                }

        if tool_name == "stream":
            try:
                import litellm
                model = args.get("model", "deepseek-chat")
                messages = args.get("messages", [])
                temperature = args.get("temperature", 0.7)
                max_tokens = args.get("max_tokens")

                # 流式调用：逐 chunk 发送 MCP notification
                collected_content = ""
                chunks_sent = 0

                response = litellm.completion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )

                for chunk in response:
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        collected_content += delta.content
                        # 发送流式 chunk 作为 notification
                        notif = {
                            "jsonrpc": "2.0",
                            "method": "notifications/stream_chunk",
                            "params": {
                                "delta": delta.content,
                                "model": model,
                            },
                        }
                        print(json.dumps(notif), flush=True)
                        chunks_sent += 1

                # 最终响应
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id", ""),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({
                                    "content": collected_content,
                                    "tool_calls": None,
                                    "finish_reason": "stop",
                                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                    "model": model,
                                    "chunks_sent": chunks_sent,
                                }),
                            }
                        ]
                    },
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id", ""),
                    "error": {"code": -32603, "message": str(e)},
                }

    # notifications/initialized 等 notification 不需要响应
    if not request.get("id"):
        return None

    # 未知方法
    return {
        "jsonrpc": "2.0",
        "id": request.get("id", ""),
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
    parser = argparse.ArgumentParser(description="Lingxi LLM litellm MCP Sidecar")
    parser.add_argument("--model", default="deepseek-chat", help="Default LLM model")
    args = parser.parse_args()

    # 从 stdin 读取 JSON-RPC 请求，向 stdout 写入响应
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(request)
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
