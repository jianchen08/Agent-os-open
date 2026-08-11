#!/usr/bin/env python3
"""
Mock MCP Server for config injection verification.

This server acts as a JSON-RPC 2.0 stdio server that:
1. Receives the `initialize` request, extracts and echoes back the `config` field
2. Responds to `notifications/on_config_change` by writing the received config to a log file
3. Responds to `tools/list` and `tools/call`

Used by verify_reproduce.py to verify the full config injection chain.
"""

import sys
import json
import os
import time

# Log file path - set via env var VERIFY_LOG_FILE
LOG_FILE = os.environ.get("VERIFY_LOG_FILE", "/tmp/mcp_verify_log.json")
RESULT_FILE = os.environ.get("VERIFY_RESULT_FILE", "/tmp/mcp_verify_result.json")


def write_result(data):
    """Write verification result data to a JSON file."""
    with open(RESULT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def handle_initialize(request):
    """Handle initialize request - extract and record the config field."""
    params = request.get("params", {})
    config = params.get("config", None)
    protocol_version = params.get("protocolVersion", "unknown")

    # Record what we received
    result_data = {
        "event": "initialize",
        "received_config": config,
        "received_protocol_version": protocol_version,
        "config_is_null": config is None,
        "config_is_object": isinstance(config, dict),
        "timestamp": time.time(),
    }
    write_result(result_data)

    # Respond with a standard MCP initialize response
    response = {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"listChanged": True},
            },
            "serverInfo": {
                "name": "mock-mcp-server",
                "version": "1.0.0",
            },
        },
    }
    return response


def handle_notification(request):
    """Handle notification - record config change notifications."""
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "notifications/on_config_change":
        config = params.get("config", None)
        result_data = {
            "event": "config_change",
            "received_config": config,
            "timestamp": time.time(),
        }
        write_result(result_data)


def handle_tools_list(request):
    """Handle tools/list."""
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": {
            "tools": [
                {
                    "name": "execute",
                    "description": "Execute pipeline plugin",
                    "inputSchema": {"type": "object"},
                }
            ]
        },
    }


def handle_tools_call(request):
    """Handle tools/call."""
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": {
            "content": [{"type": "text", "text": "mock execution result"}],
            "state_updates": {},
            "route_signal": None,
            "skip_remaining": False,
        },
    }


def main():
    """Main loop - read JSON-RPC from stdin, respond on stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method", "")

        # Notifications have no "id" field
        if "id" not in request:
            handle_notification(request)
            continue

        if method == "initialize":
            response = handle_initialize(request)
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        elif method == "tools/list":
            response = handle_tools_list(request)
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        elif method == "tools/call":
            response = handle_tools_call(request)
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        else:
            # Unknown method - return error
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
