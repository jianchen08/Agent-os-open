"""
判据 A：Python 只读 WS 延迟探针（零副作用）

目的：切分"后端 send/网络"vs"前端主线程"。
Python 客户端不含浏览器主线程，它看到的 latency = recv_ts - __send_ts 只包含
后端 ws.send_text 排队 + 网络 + 本地 socket，与前端 [WS_TRACE] RECV 的同口径值
形成对照：
  - 若本探针 latency 也高（≈ 前端报的 5xx ms）→ 主因在后端/网络
  - 若本探针 latency 很低（<100ms）→ 主因在前端主线程

纯只读：不发任何业务消息，不触发 LLM，只被动收 60s，统计后退出。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from urllib.request import Request, urlopen

from websockets import connect

API = "http://localhost:8988"
WS_URL = "ws://localhost:8988/ws/chat"
USER, PWD = "admin", "admin123"
OBSERVE_SECONDS = 60


def login() -> str:
    req = Request(
        f"{API}/api/v1/auth/login",
        data=json.dumps({"username": USER, "password": PWD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


async def observe(token: str) -> None:
    url = f"{WS_URL}?token={token}"
    by_type: Counter[str] = Counter()
    by_pid: Counter[str] = Counter()
    latencies: list[float] = []
    slow: list[dict] = []
    total = 0
    t0 = time.time()
    async with connect(url, max_size=None) as ws:
        print(f"[probe] 已连后端 WS，被动观察 {OBSERVE_SECONDS}s ...")
        try:
            while time.time() - t0 < OBSERVE_SECONDS:
                raw = await asyncio.wait_for(ws.recv(), timeout=OBSERVE_SECONDS)
                total += 1
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                typ = d.get("type", "?")
                data = d.get("data") if isinstance(d.get("data"), dict) else {}
                pid = (data.get("pipeline_id") or "")[:12]
                by_type[typ] += 1
                by_pid[pid or "(空)"] += 1
                send_ts = d.get("__send_ts") or data.get("__send_ts")
                if isinstance(send_ts, (int, float)):
                    lat = time.time() * 1000 - send_ts
                    latencies.append(lat)
                    if lat > 200 and len(slow) < 15:
                        slow.append(
                            {"type": typ, "pid": pid, "lat_ms": round(lat, 1)}
                        )
        except asyncio.TimeoutError:
            pass

    elapsed = time.time() - t0
    print(f"\n===== 判据 A 结果（观察 {elapsed:.1f}s）=====")
    print(f"总帧数: {total}")
    print(f"带 __send_ts 帧数: {len(latencies)}（仅这些可测延迟）")
    print("\n帧类型分布:")
    for t, c in by_type.most_common():
        print(f"  {t:28} {c}")
    print("\n帧 pipeline 分布(前10):")
    for p, c in by_pid.most_common(10):
        print(f"  {p:16} {c}")

    if latencies:
        latencies.sort()
        n = len(latencies)
        avg = sum(latencies) / n
        p50 = latencies[n // 2]
        p95 = latencies[int(n * 0.95)]
        p99 = latencies[min(n - 1, int(n * 0.99))]
        print("\n★ Python 客户端 latency (recv - __send_ts) 毫秒:")
        print(f"  min={latencies[0]:.1f}  p50={p50:.1f}  p95={p95:.1f}  p99={p99:.1f}  max={latencies[-1]:.1f}  avg={avg:.1f}")
        print(f"  >200ms 帧数: {sum(1 for x in latencies if x > 200)} / {n}")
        print(f"  >500ms 帧数: {sum(1 for x in latencies if x > 500)} / {n}")
        print("\n  样例(>200ms):")
        for s in slow:
            print(f"    {s}")
    else:
        print("\n⚠️ 60s 内没收到任何带 __send_ts 的业务帧（可能当前无并发任务在推流）")

    print("\n解读:")
    print("  - 若 p95 > 300ms → 后端/网络慢，前端主线程不是主因")
    print("  - 若 p95 < 100ms → 主因在前端主线程，去跑判据 B(浏览器)")


if __name__ == "__main__":
    asyncio.run(observe(login()))
