#!/usr/bin/env python3
"""E2E 端到端验证脚本 - 完整插件集
启动 kernel，发送 chat 请求，捕获完整响应并做断言。
"""
import subprocess, time, httpx, json, sys, os, signal

def cleanup():
    subprocess.run(["pkill", "-f", "agentos-kernel"], capture_output=True)
    subprocess.run(["pkill", "-f", "python3.*server.py"], capture_output=True)
    time.sleep(3)

def main():
    cleanup()
    
    env = os.environ.copy()
    env["AGENTOS_KERNEL_PORT"] = "9100"
    env["AGENTOS_PLUGINS_DIR"] = "plugins/shared"
    env["RUST_LOG"] = "info"
    
    log_file = open("/tmp/kernel_full.log", "w")
    proc = subprocess.Popen(
        ["kernel/target/release/agentos-kernel"],
        stdout=log_file, stderr=subprocess.STDOUT, env=env
    )
    print(f"[1] Kernel started PID={proc.pid}", flush=True)
    
    # Wait for startup
    time.sleep(12)
    
    # Health check
    try:
        r = httpx.get("http://127.0.0.1:9100/health", timeout=10)
        print(f"[2] Health: {r.status_code} - {r.json()['status']}", flush=True)
    except Exception as e:
        print(f"[2] Health check FAILED: {e}", flush=True)
        log_file.close()
        proc.kill()
        sys.exit(1)
    
    # Verify plugins loaded
    time.sleep(1)
    log_file.flush()
    with open("/tmp/kernel_full.log") as f:
        log_content = f.read()
    plugin_count = log_content.count("Manifest validated:")
    tool_count = log_content.count("Tool registered:")
    print(f"[3] Plugins discovered: {plugin_count}, Tools registered: {tool_count}", flush=True)
    
    # Send chat request with 600s timeout (44 plugins, some may timeout 30s each)
    print(f"[4] Sending chat request (timeout=600s)...", flush=True)
    start_time = time.time()
    try:
        r = httpx.post("http://127.0.0.1:9100/api/v1/chat", 
                        json={"message": "hello", "session_id": "e2e-full-001"},
                        timeout=600.0)
        elapsed = time.time() - start_time
        print(f"[5] Response received in {elapsed:.1f}s, status={r.status_code}", flush=True)
        print(f"[6] Response body: {r.text}", flush=True)
        
        parsed = r.json()
        content = parsed.get("content", "")
        
        # Assertions
        print(f"\n=== ASSERTIONS ===", flush=True)
        
        check_noop = "NOOP_INVOKER" not in content
        print(f"[A1] Response does NOT contain NOOP_INVOKER: {'PASS' if check_noop else 'FAIL'}", flush=True)
        
        check_no_plugin = "no_plugin_executed" not in content
        print(f"[A2] Response does NOT contain no_plugin_executed: {'PASS' if check_no_plugin else 'FAIL'}", flush=True)
        
        check_steps = "steps_executed" in content
        print(f"[A3] Response contains steps_executed: {'PASS' if check_steps else 'INFO'}", flush=True)
        
        # Save response
        with open("/tmp/e2e_full_response.json", "w") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
        print(f"\n[7] Response saved to /tmp/e2e_full_response.json", flush=True)
        
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[5] Request FAILED after {elapsed:.1f}s: {e}", flush=True)
    
    # Analyze kernel log
    log_file.close()
    time.sleep(1)
    with open("/tmp/kernel_full.log") as f:
        log_lines = f.readlines()
    
    mcp_connected = sum(1 for l in log_lines if "MCP client connected" in l)
    mcp_init_failed = sum(1 for l in l if "MCP_INIT_FAILED" in l) if False else sum(1 for l in log_lines if "MCP_INIT_FAILED" in l)
    step_completed = sum(1 for l in log_lines if "Step completed" in l)
    
    print(f"\n=== KERNEL LOG ANALYSIS ===", flush=True)
    print(f"MCP client connected: {mcp_connected}", flush=True)
    print(f"MCP_INIT_FAILED: {mcp_init_failed}", flush=True)
    print(f"Step completed: {step_completed}", flush=True)
    print(f"NOOP_INVOKER in log: {'NOOP_INVOKER' in open('/tmp/kernel_full.log').read()}", flush=True)
    
    # Cleanup
    proc.kill()
    proc.wait()
    print(f"\n[8] Kernel killed. Done.", flush=True)

if __name__ == "__main__":
    main()
