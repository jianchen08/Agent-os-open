#!/usr/bin/env python3
"""
前端容器 server.py WS 代理链路对比验证
======================================
目标：实证「前端未连接」根因——容器前端代理目标端口配置错配。

场景 A（正确配置）：浏览器 → server.py:5290 (BACKEND_WS_URL=ws://127.0.0.1:9100) → kernel
   期望：WS 握手成功（connection_confirmation），链路通
场景 B（start_web_cn.bat 默认错配）：浏览器 → server.py:5291 (BACKEND_WS_URL=ws://127.0.0.1:8988) → 无服务
   期望：WS 握手失败（连接拒绝/超时）→ 复现前端「未连接」

同时验证 HTTP 代理 /health 与 /api/v1/auth/login 在两种配置下的表现。
"""
import asyncio
import json
import urllib.request
import urllib.error

import websockets

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def http_get(port: int, path: str, timeout: int = 5):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode()[:100]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:100]
    except Exception as e:
        return 0, str(e)[:100]


async def ws_probe(port: int, token: str | None) -> tuple[int, str]:
    """通过 server.py 代理连 WS，返回 (成功标志, 详情)。"""
    url = f"ws://127.0.0.1:{port}/ws/chat"
    if token is not None:
        url += f"?token={token}"
    try:
        async with websockets.connect(url, open_timeout=6, close_timeout=3) as ws:
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=4)
                return 1, first[:120]
            except asyncio.TimeoutError:
                return 0, "(握手后 4s 无消息)"
    except Exception as e:
        return 0, str(e)[:150]


async def main():
    # 先拿真实 token（走实例 A 的 /api 代理 → kernel:9100）
    def login_via(port: int):
        url = f"http://127.0.0.1:{port}/api/v1/auth/login"
        data = json.dumps({"username": "admin", "password": "admin12345"}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                body = json.loads(resp.read().decode())
                return resp.status, body.get("access_token", "")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            return 0, str(e)[:80]

    # ── 场景 A：代理目标 9100（正确）──
    print("===== 场景 A：BACKEND_WS_URL=ws://127.0.0.1:9100（正确配置，端口 5290） =====")
    st, body = http_get(5290, "/health")
    check("A1 server.py /health", st == 200, f"status={st} body={body}")

    st_a, token_a = login_via(5290)
    check("A2 login 经代理→kernel", st_a == 200 and bool(token_a),
          f"status={st_a} has_token={bool(token_a)}")

    if token_a:
        ok, detail = await ws_probe(5290, token_a)
        check("A3 WS 经代理→kernel 握手", ok == 1, f"detail={detail}")
    else:
        check("A3 WS 经代理→kernel 握手", False, "无 token 跳过")

    # ── 场景 B：代理目标 8988（start_web_cn.bat 默认 BACKEND_PORT=8988，错配）──
    print("\n===== 场景 B：BACKEND_WS_URL=ws://127.0.0.1:8988（start_web_cn.bat 默认，端口 5291） =====")
    st, body = http_get(5291, "/health")
    check("B1 server.py /health", st == 200, f"status={st} body={body}")

    st_b, token_b = login_via(5291)
    check("B2 login 经代理→8988", st_b == 200 and bool(token_b),
          f"status={st_b} has_token={bool(token_b)}")

    ok, detail = await ws_probe(5291, token_b or "fake")
    check("B3 WS 经代理→8988 握手", ok == 1, f"detail={detail}")

    print("\n==== 汇总 ====")
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys_exit = 0 if not FAIL else 1
    import sys
    sys.exit(sys_exit)


if __name__ == "__main__":
    asyncio.run(main())
