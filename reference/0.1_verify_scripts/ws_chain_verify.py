#!/usr/bin/env python3
"""
WS 连接链路真实验证脚本
=======================
目标：复现「前端未连接 + 发送消息无反应」根因，验证 WS 链路各环节。

验证内容：
  V1. kernel /health 是否可达（端口 9100）
  V2. POST /api/v1/auth/login 拿 token（admin/admin12345）
  V3. WS /ws/chat?token=xxx 握手：期望 connection_confirmation
  V4. WS 无 token：期望 4001 拒绝
  V5. WS 假 token：期望 4001 拒绝
  V6. WS heartbeat → heartbeat_ack
  V7. WS user_input → 有响应（stream_start 或 new_message 或错误，证明链路通）

用法：python3 docs/working/ws_chain_verify.py [--port 9100]
前置：kernel 已启动（本脚本也可 --start-kernel 自动拉起）
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

import websockets

PASS, FAIL, WARN = [], [], []


def check(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    (PASS if ok else FAIL).append(name)
    print(f"[{tag}] {name} {detail}")
    return ok


def http_json(method: str, port: int, path: str, body: dict | None = None,
              token: str | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


async def ws_handshake(port: int, token: str | None) -> tuple[int, str]:
    """尝试 WS 握手，返回 (close_code, 收到的首条消息/原因)。"""
    url = f"ws://127.0.0.1:{port}/ws/chat"
    if token is not None:
        url += f"?token={token}"
    try:
        async with websockets.connect(url, open_timeout=8, close_timeout=3) as ws:
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=5)
                return 0, first
            except asyncio.TimeoutError:
                return 0, "(no message within 5s)"
    except websockets.exceptions.InvalidStatus as e:
        return getattr(e, "status_code", -1), str(e)[:200]
    except Exception as e:
        return -1, str(e)[:200]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--start-kernel", action="store_true")
    args = ap.parse_args()
    port = args.port

    proc = None
    if args.start_kernel:
        print("[INFO] 启动 kernel...")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) or "."
        env = os.environ.copy()
        env["AGENTOS_KERNEL_PORT"] = str(port)
        env["AGENTOS_KERNEL_HOST"] = "0.0.0.0"
        env.setdefault("AGENTOS_PLUGINS_DIR", os.path.join(root, "plugins/shared"))
        env.setdefault("AGENTOS_CONFIG_ROOT", os.path.join(root, "config"))
        proc = subprocess.Popen(
            [os.path.join(root, "kernel/target/debug/agentos-kernel")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=root,
        )
        for i in range(60):
            st, _ = http_json("GET", port, "/health")
            if st == 200:
                print(f"[INFO] kernel /health 就绪 (attempt {i+1})")
                break
            time.sleep(1)
        else:
            check("kernel /health 可达", False, "60s 未就绪")
            proc.terminate()
            sys.exit(1)

    # V1: /health
    st, body = http_json("GET", port, "/health")
    check("V1 /health 可达", st == 200, f"status={st} body={body}")

    # V2: login
    st, body = http_json("POST", port, "/api/v1/auth/login",
                         {"username": "admin", "password": "admin12345"})
    token = body.get("access_token", "") if isinstance(body, dict) else ""
    check("V2 login 拿 token", st == 200 and bool(token),
          f"status={st} has_token={bool(token)}")

    # V3: WS 带 token 握手
    code, msg = await ws_handshake(port, token)
    check("V3 WS 带token握手", code == 0 and "connection_confirmation" in msg,
          f"code={code} msg={msg[:120]}")

    # V4: WS 无 token
    code, msg = await ws_handshake(port, None)
    check("V4 WS 无token拒绝", code != 0,
          f"code={code} msg={msg[:120]}")

    # V5: WS 假 token
    code, msg = await ws_handshake(port, "fake-token-123")
    check("V5 WS 假token拒绝", code != 0,
          f"code={code} msg={msg[:120]}")

    # V6+V7: 需要真实连接做双向收发
    if token:
        url = f"ws://127.0.0.1:{port}/ws/chat?token={token}"
        try:
            async with websockets.connect(url, open_timeout=8) as ws:
                try:
                    conf = await asyncio.wait_for(ws.recv(), timeout=5)
                    print(f"[INFO] connection_confirmation: {conf[:150]}")
                except asyncio.TimeoutError:
                    print("[WARN] 未收到 connection_confirmation")

                # V6: heartbeat
                await ws.send(json.dumps({"type": "heartbeat", "timestamp": int(time.time() * 1000)}))
                try:
                    hb = await asyncio.wait_for(ws.recv(), timeout=5)
                    hb_ok = "heartbeat_ack" in hb
                except asyncio.TimeoutError:
                    hb = "(no ack within 5s)"
                    hb_ok = False
                check("V6 heartbeat→ack", hb_ok, f"got={hb[:120]}")

                # V7: user_input（需要 thread_id）
                await ws.send(json.dumps({
                    "type": "user_input",
                    "thread_id": "verify_thread",
                    "content": "ping",
                    "pipeline_id": "verify_pipeline",
                    "attachments": [],
                    "enable_thinking": False,
                    "client_message_id": "verify-001",
                }))
                responses = []
                try:
                    for _ in range(5):
                        r = await asyncio.wait_for(ws.recv(), timeout=8)
                        responses.append(r)
                        print(f"[INFO] user_input resp: {r[:150]}")
                        if "new_message" in r or "stream_error" in r:
                            break
                except asyncio.TimeoutError:
                    pass
                check("V7 user_input 有响应", len(responses) > 0,
                      f"responses={len(responses)}")
        except Exception as e:
            check("V6+V7 双向收发", False, f"连接失败: {e}")

    if proc:
        proc.terminate()

    print("\n==== 汇总 ====")
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}  WARN: {len(WARN)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    asyncio.run(main())
