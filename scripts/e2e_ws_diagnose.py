"""E2E 诊断：捕获 WS 关闭码 + 排查流式事件丢失。

针对 e2e_ws_tool_flow.py 暴露的两个现象做精确诊断：
1. WS 发送后立即 "连接关闭" —— 抓取真实 close code / reason。
2. tool_start/tool_result 流式事件未送达 —— 检查 sink 注册与心跳存活。

关键修正：原脚本用 ws.recv() 超时循环，收 connection_confirmation 后若 10s 无事件就
误判。本脚本：先排空 confirmation，发送后最长等 60s，打印每一个事件类型，并打印
close.code/reason。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
import urllib.error
from collections import Counter

import websockets

BASE = "http://localhost:8988"
WS_BASE = "ws://localhost:8988"


def http(path: str, method: str = "GET", token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "{}")


async def diagnose(token: str, thread_id: str, pipeline_id: str) -> None:
    uri = f"{WS_BASE}/ws/chat?token={token}"
    print(f"\n[WS] 连接 {WS_BASE}/ws/chat?token=...")

    user_msg = {
        "type": "user_input",
        "thread_id": thread_id,
        "content": "请读取 /tmp/e2e_diag_file.txt 文件的内容",
        "pipeline_id": pipeline_id,
        "attachments": [],
        "enable_thinking": False,
        "client_message_id": f"diag-{int(time.time())}",
    }

    event_counter: Counter[str] = Counter()
    close_info: dict = {}
    deadline = time.time() + 60

    try:
        async with websockets.connect(uri, max_size=2**20, ping_interval=30) as ws:
            print("[WS] 已 accept，等待 connection_confirmation ...")
            # 第 1 条必定是 connection_confirmation
            try:
                raw0 = await asyncio.wait_for(ws.recv(), timeout=10)
                evt0 = json.loads(raw0)
                print(f"[WS] 首帧 type={evt0.get('type')} data={json.dumps(evt0.get('data'), ensure_ascii=False)[:120]}")
            except asyncio.TimeoutError:
                print("[WS] [WARN] 10s 内未收到 connection_confirmation")

            # 注册到线程连接池（关键：sink 按 thread_id 投递流式事件）
            print(f"[WS] 发送 user_input（pipeline_id={pipeline_id}）")
            await ws.send(json.dumps(user_msg))

            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    # 探活：发心跳看服务端是否还认这条连接
                    try:
                        await ws.send(json.dumps({"type": "heartbeat", "thread_id": thread_id}))
                    except Exception as e:
                        print(f"[WS] 心跳发送失败: {e}")
                    print(f"[WS] 5s 无事件（累计事件: {dict(event_counter)}），已发心跳探活...")
                    continue
                except websockets.ConnectionClosed as cc:
                    close_info = {"code": cc.code, "reason": cc.reason}
                    print(f"\n[WS] [CLOSED] code={cc.code} reason={cc.reason!r}")
                    break

                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[WS] 非JSON帧: {raw[:80]!r}")
                    continue
                etype = evt.get("type", "?")
                data = evt.get("data", {})
                event_counter[etype] += 1
                # 重点事件全量打印
                if etype in ("tool_start", "tool_result", "stream_start", "stream_end",
                             "new_message", "state_change", "stream_error", "error"):
                    snippet = json.dumps(data, ensure_ascii=False)[:160]
                    print(f"[EVENT] {etype}: {snippet}")
                    if etype == "state_change":
                        # 终态：停止轮询
                        print("[WS] 收到 state_change，视为本轮结束")
                        break
                elif etype == "heartbeat_ack":
                    print(f"[WS] heartbeat_ack（连接存活）")
    except Exception as exc:
        print(f"\n[WS] 连接层异常: {type(exc).__name__}: {exc}")
        return

    print("\n" + "=" * 50)
    print(f"事件统计: {dict(event_counter)}")
    if close_info:
        print(f"关闭信息: code={close_info['code']} reason={close_info['reason']!r}")
        # 4001=token问题, 1006=异常断开, 1011=服务端错误, 1000=正常关闭
        code = int(close_info["code"]) if close_info["code"] else 0
        if code == 4001:
            print("  >>> 诊断: token 认证被拒（过期/无效）")
        elif code in (1006, 1011):
            print("  >>> 诊断: 异常断开（服务端异常或网络），需查后端日志")
        elif code == 1000:
            print("  >>> 诊断: 服务端主动正常关闭")
    else:
        print("关闭信息: 无（连接仍在 deadline 内存活）")


async def main() -> None:
    print("=" * 60)
    print("E2E WS 关闭码 + 流式事件 诊断")
    print("=" * 60)

    code, data = http("/api/v1/auth/login", "POST", body={"username": "admin", "password": "admin123"})
    assert code == 200, f"登录失败: {code}"
    token = data["access_token"]
    print("[OK] 登录成功")

    code, data = http("/api/v1/threads", "POST", token=token,
                      body={"title": "E2E-WS诊断", "intent": "诊断WS关闭"})
    assert code in (200, 201), f"创建会话失败: {code} {data}"
    thread_id = data["thread_id"]
    pipeline_id = (data.get("pipeline_ids") or [None])[0]
    if not pipeline_id:
        import time as _t
        for _ in range(10):
            _t.sleep(1)
            _, d = http(f"/api/v1/threads/{thread_id}", token=token)
            pids = d.get("pipeline_ids") or []
            if pids:
                pipeline_id = pids[0]
                break
    assert pipeline_id, "主管道未就绪"
    print(f"[OK] 会话={thread_id} 管道={pipeline_id}")

    await diagnose(token, thread_id, pipeline_id)
    print("\n诊断完成")


if __name__ == "__main__":
    asyncio.run(main())
