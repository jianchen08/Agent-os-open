# Lingxi LLM Sidecar — Python litellm MCP Server
#
# 通过 JSON-RPC over stdio 提供 LLM 调用能力。
# 内核通过 McpClient 连接此边车进程，调用 tools/call 执行 LLM 请求。
#
# 依赖 python 包 litellm（pip install litellm）；未安装时工具调用直接返回
# JSON-RPC error，不做 mock 降级。流式调用请使用 litellm_proxy.py（真实流式）。
#
# AC-06-2: Python litellm 边车可被内核通过 MCP 调用
#
# [来源: docs/tasks/task_07_llm_api.md]

import sys
import json
import os

# 与 config/models/llm.yaml defaults.call_timeout 对齐（单位：秒）
CALL_TIMEOUT_SECONDS = 600

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
        # 本服务未实现真实流式（曾经的实现是把用户输入切块伪装成 chunk）。
        # 已从 tools/list 移除；此处仅对旧调用方返回明确的 not-supported 错误。
        # 真实流式请调用 litellm_proxy.py 的 stream 工具。
        return {
            "error": {
                "code": -32601,
                "message": (
                    "complete_stream is not supported by this server "
                    "(no real streaming implementation); "
                    "use mcp-servers/llm-sidecar/litellm_proxy.py 'stream' tool instead"
                ),
            }
        }
    elif name == "list_models":
        return handle_list_models()
    else:
        return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}

def handle_complete(args):
    model = args.get("model", "deepseek-chat")
    messages = args.get("messages", [])

    try:
        import litellm
    except ImportError as e:
        # fail-fast：不做 mock 降级，直接返回错误（沿用本文件 JSON-RPC error 格式）
        return {
            "error": {
                "code": -32603,
                "message": f"litellm is required but not installed: {e}. pip install litellm",
            }
        }

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=args.get("temperature", 0.7),
            max_tokens=args.get("max_tokens", 4096),
            timeout=CALL_TIMEOUT_SECONDS,
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
    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": f"LLM error: {e}"}]}

    return {
        "content": [{"type": "text", "text": json.dumps(result)}]
    }

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
