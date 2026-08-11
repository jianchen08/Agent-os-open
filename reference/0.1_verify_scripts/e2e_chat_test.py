#!/usr/bin/env python3
"""E2E chat test - sends request to kernel and captures response."""
import sys
import httpx
import json

url = "http://127.0.0.1:9100/api/v1/chat"
payload = {"message": "hello", "session_id": "e2e-pytest-001"}

print(f"Sending POST to {url}...", flush=True)
try:
    resp = httpx.post(url, json=payload, timeout=600.0)
    print(f"Status: {resp.status_code}", flush=True)
    print(f"Response body:", flush=True)
    print(resp.text, flush=True)
    try:
        parsed = resp.json()
        print(f"\nParsed JSON:", flush=True)
        print(json.dumps(parsed, indent=2, ensure_ascii=False), flush=True)
        content = parsed.get("content", "")
        
        # Assertions
        print("\n=== ASSERTIONS ===", flush=True)
        if "NOOP_INVOKER" in content:
            print("FAIL: Response contains NOOP_INVOKER", flush=True)
        else:
            print("PASS: Response does NOT contain NOOP_INVOKER", flush=True)
            
        if "no_plugin_executed" in content:
            print("FAIL: Response contains no_plugin_executed", flush=True)
        else:
            print("PASS: Response does NOT contain no_plugin_executed", flush=True)
            
        if "steps_executed" in content:
            print("PASS: Response contains steps_executed (plugins executed)", flush=True)
        elif "MCP_INIT_FAILED" in content or "MCP_CONNECT_FAILED" in content:
            print("INFO: Response contains MCP error codes (not NOOP)", flush=True)
            
    except Exception as e:
        print(f"JSON parse error: {e}", flush=True)
except Exception as e:
    print(f"Request failed: {e}", flush=True)
    sys.exit(1)
