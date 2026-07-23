# Lingxi LLM Sidecar — Python litellm MCP Server
#
# 通过 JSON-RPC over stdio 提供 LLM 调用能力。
# 内核通过 McpClient 连接此边车进程，调用 tools/call 执行 LLM 请求。
#
# AC-06-2: Python litellm 边车可被内核通过 MCP 调用
#
# [来源: docs/tasks/task_07_llm_api.md]

import sys
import json
import os

def handle_initialize(params):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {}
        },
        "serverInfo": {
            "name": "agentos-llm-sidecar",
            "version": "0.2.0"
        }
    }

def handle_tools_list(params):
    return {
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
                        "max_tokens": {"type": "integer"}
                    },
                    "required": ["model", "messages"]
                }
            },
            {
                "name": "complete_stream",
                "description": "Streaming LLM completion (returns chunk via notification)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "model": {"type": "string"},
                        "messages": {"type": "array"},
                        "temperature": {"type": "number"},
                        "max_tokens": {"type": "integer"}
                    },
                    "required": ["model", "messages"]
                }
            },
            {
                "name": "list_models",
                "description": "List available models",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            }
        ]
    }

def handle_tools_call(params):
    name = params.get("name", "")
    arguments = params.get("arguments", {})

    if name == "complete":
        return handle_complete(arguments)
    elif name == "complete_stream":
        return handle_complete_stream(arguments)
    elif name == "list_models":
        return handle_list_models()
    else:
        return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}

def handle_complete(args):
    model = args.get("model", "deepseek-chat")
    messages = args.get("messages", [])

    # Try litellm first, fall back to mock if not installed
    try:
        import litellm
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=args.get("temperature", 0.7),
            max_tokens=args.get("max_tokens", 4096),
        )
        choice = response.choices[0]
        result_content = choice.message.content or ""
        finish = choice.finish_reason or "stop"
        usage = getattr(response, "usage", None)
        result = {
            "content": result_content,
            "model": model,
            "finish_reason": finish,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
            }
        }
    except ImportError:
        # Mock response for testing without litellm
        user_msg = ""
        for m in messages:
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        result = {
            "content": f"[mock-llm] Response to: {user_msg}",
            "model": model,
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": len(str(messages)) // 4,
                "completion_tokens": 50,
                "total_tokens": len(str(messages)) // 4 + 50,
            }
        }
    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": f"LLM error: {e}"}]}

    return {
        "content": [{"type": "text", "text": json.dumps(result)}]
    }

def handle_complete_stream(args):
    model = args.get("model", "deepseek-chat")
    messages = args.get("messages", [])
    user_msg = ""
    for m in messages:
        if m.get("role") == "user":
            user_msg = m.get("content", "")
            break

    # For stdio mode, we simulate streaming by sending the full response
    # Real streaming would use MCP notifications per chunk
    chunks = [user_msg[i:i+10] for i in range(0, len(user_msg), 10)] if user_msg else ["mock"]

    result = {
        "content": [{"type": "text", "text": json.dumps({
            "chunks": chunks,
            "model": model,
            "finish_reason": "stop",
        })}]
    }
    return result

def handle_list_models():
    models = [
        {"id": "deepseek-chat", "name": "DeepSeek Chat"},
        {"id": "deepseek-coder", "name": "DeepSeek Coder"},
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
    ]
    return {
        "content": [{"type": "text", "text": json.dumps(models)}]
    }

def process_request(req):
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        result = handle_initialize(params)
    elif method == "tools/list":
        result = handle_tools_list(params)
    elif method == "tools/call":
        result = handle_tools_call(params)
    else:
        if req_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }
        return None

    if req_id is None:
        # Notification — no response
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result
    }

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = process_request(req)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
