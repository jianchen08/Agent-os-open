"""端到端 WS 推送延迟测量脚本。

连真实运行的后端（默认 localhost:8988），发一条消息触发流式响应，
测量每个事件从后端推送到客户端接收的真实延迟。

测量三类数据：
1. 相邻事件到达间隔（间隔大 = 该时段没有事件 = 用户感知"卡住"）
2. 首个 stream_start 到首个内容事件（stream_chunk/thinking_chunk）的间隔
   —— 即"转圈时长"，复现用户报告的"转圈 4-5 秒后一次弹出"
3. 如果后端事件携带 __send_ts，算端到端延迟（需后端重启加载埋点后才有效）

用法：
  python tests/e2e_ws_latency_probe.py
  python tests/e2e_ws_latency_probe.py --host localhost --port 8988 --rounds 3

判据：
  - 若 stream_start 到首个 chunk 间隔 > 2s：后端 LLM 首 token 慢（thinking 期）
  - 若某段相邻事件间隔突增：后端在该段没推事件（事件循环被占 / LLM 卡）
  - 若 __send_ts 延迟大：网络/浏览器侧（需后端埋点生效）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime

try:
    import aiohttp
except ImportError:
    print("ERROR: 需要 aiohttp: pip install aiohttp", file=sys.stderr)
    sys.exit(1)


DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8988
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin123"
MAX_WAIT_PER_EVENT = 180  # 单条事件最长等待（秒），覆盖长思考


async def login(host: str, port: int, user: str, password: str) -> str:
    """登录获取 access_token。"""
    url = f"http://{host}:{port}/api/v1/auth/login"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json={"username": user, "password": password},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"登录失败 status={resp.status} body={body}")
            data = await resp.json()
            return data["access_token"]


async def probe_one_round(
    session: aiohttp.ClientSession,
    ws_url: str,
    prompt: str,
) -> dict:
    """跑一轮：连接 → 发消息 → 收集所有事件时序 → 返回测量结果。"""
    t_connect_start = time.time()
    events: list[dict] = []  # 每个: {type, recv_ts, send_ts(可选), content_len, gap_ms}

    async with session.ws_connect(ws_url, heartbeat=30) as ws:
        t_connected = time.time()

        # 等待 connection_confirmation
        first = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert first.get("type") == "connection_confirmation", (
            f"期望 connection_confirmation，收到 {first.get('type')}"
        )
        t_confirmed = time.time()

        # 发送用户消息
        # /ws/chat 全局端点要求消息体携带 thread_id（app_factory.py:311），
        # 否则立即返回 error。thread_id 同时作为 pipeline_id（主会话场景二者一致）。
        thread_id = f"latency-probe-{int(t_connect_start * 1000)}"
        user_msg = {
            "type": "user_input",
            "data": {
                "content": prompt,
                "thread_id": thread_id,
                "pipeline_id": thread_id,
            },
        }
        t_sent = time.time()
        await ws.send_json(user_msg)

        # 收集事件
        prev_recv_ts = t_sent
        first_content_ts: float | None = None
        stream_start_ts: float | None = None
        got_end = False
        deadline = time.time() + 200  # 整轮最多 200s

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    ws.receive(), timeout=MAX_WAIT_PER_EVENT,
                )
            except asyncio.TimeoutError:
                events.append({
                    "type": "_TIMEOUT",
                    "recv_ts": time.time(),
                    "gap_ms": (time.time() - prev_recv_ts) * 1000,
                    "note": f"等待事件超时 {MAX_WAIT_PER_EVENT}s",
                })
                break

            if raw.type == aiohttp.WSMsgType.TEXT:
                t_recv = time.time()
                gap_ms = (t_recv - prev_recv_ts) * 1000
                prev_recv_ts = t_recv
                try:
                    data = json.loads(raw.data)
                except json.JSONDecodeError:
                    continue
                etype = data.get("type", "unknown")
                data_inner = data.get("data", {}) or {}
                send_ts = data.get("__send_ts") or data_inner.get("__send_ts")
                content = data_inner.get("content", "")
                events.append({
                    "type": etype,
                    "recv_ts": t_recv,
                    "send_ts": send_ts,
                    "content_len": len(content) if isinstance(content, str) else 0,
                    "gap_ms": gap_ms,
                })

                if etype == "stream_start":
                    stream_start_ts = t_recv
                elif etype in ("stream_chunk", "thinking_chunk") and first_content_ts is None:
                    first_content_ts = t_recv
                elif etype in ("stream_end", "new_message"):
                    got_end = True
                    # new_message 后通常结束，再短暂收尾
                    if etype == "new_message":
                        break
            elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                events.append({
                    "type": f"_WS_{raw.type.name}",
                    "recv_ts": time.time(),
                    "gap_ms": (time.time() - prev_recv_ts) * 1000,
                })
                break

    # 关键时序指标
    spin_duration_ms = None
    if stream_start_ts and first_content_ts:
        spin_duration_ms = (first_content_ts - stream_start_ts) * 1000

    return {
        "connect_ms": (t_connected - t_connect_start) * 1000,
        "confirm_ms": (t_confirmed - t_connected) * 1000,
        "send_to_first_event_ms": (events[0]["recv_ts"] - t_sent) * 1000 if events else None,
        "spin_duration_ms": spin_duration_ms,  # stream_start → 首个内容
        "events": events,
        "got_end": got_end,
    }


def print_round_report(idx: int, prompt: str, result: dict) -> None:
    """打印单轮测量报告。"""
    print(f"\n{'=' * 70}")
    print(f"第 {idx} 轮 | prompt: 「{prompt}」")
    print(f"{'=' * 70}")
    print(f"  连接耗时:     {result['connect_ms']:.1f}ms")
    print(f"  确认耗时:     {result['confirm_ms']:.1f}ms")
    if result["send_to_first_event_ms"] is not None:
        print(f"  发送→首事件:  {result['send_to_first_event_ms']:.1f}ms")
    if result["spin_duration_ms"] is not None:
        marker = " ⚠️⚠️ 这就是'转圈时长'" if result["spin_duration_ms"] > 2000 else ""
        print(f"  ★ 转圈时长(stream_start→首内容): {result['spin_duration_ms']:.0f}ms{marker}")
    print(f"  收到结束事件: {'是' if result['got_end'] else '否（超时/中断）'}")

    # 事件类型统计
    type_counts = Counter(e["type"] for e in result["events"])
    print(f"\n  事件类型统计:")
    for etype, cnt in type_counts.most_common():
        print(f"    {etype}: {cnt}")

    # 相邻事件间隔 — 重点关注大间隔（卡顿段）
    big_gaps = [
        (i, e) for i, e in enumerate(result["events"])
        if e.get("gap_ms", 0) > 1000  # >1s 的间隔
    ]
    if big_gaps:
        print(f"\n  ⚠️ 大间隔事件（>1000ms 的卡顿段，共 {len(big_gaps)} 处）:")
        for i, e in big_gaps:
            prev_type = result["events"][i - 1]["type"] if i > 0 else "(开始)"
            print(f"    [{i}] {prev_type} → {e['type']}: 间隔 {e['gap_ms']:.0f}ms")

    # 端到端延迟（如果后端注入了 __send_ts）
    latency_events = [
        e for e in result["events"]
        if e.get("send_ts") and e.get("recv_ts")
    ]
    if latency_events:
        print(f"\n  端到端延迟（后端发送→客户端接收，基于 __send_ts，共 {len(latency_events)} 条）:")
        latencies = []
        for e in latency_events:
            lat = e["recv_ts"] * 1000 - e["send_ts"]
            latencies.append((e["type"], lat))
            if lat > 500:
                print(f"    ⚠️ {e['type']}: {lat:.0f}ms")
        if latencies:
            vals = [l for _, l in latencies]
            print(f"    min={min(vals):.0f}ms  max={max(vals):.0f}ms  "
                  f"avg={sum(vals) / len(vals):.0f}ms")
    else:
        print("\n  (未检测到 __send_ts —— 后端埋点未生效，需重启后端加载埋点)")
        print("  当前只能看相邻事件间隔；端到端延迟需后端重启后重跑")


async def main() -> int:
    parser = argparse.ArgumentParser(description="WS 推送延迟测量")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument(
        "--prompt", default="回复两个字：你好",
        help="发送的测试消息（默认简单问候，触发流式回复）",
    )
    parser.add_argument("--rounds", type=int, default=2, help="测量轮数")
    args = parser.parse_args()

    print(f"WS 推送延迟测量 | {datetime.now().isoformat()}")
    print(f"后端: ws://{args.host}:{args.port}/ws/chat")
    print(f"账号: {args.user} | 轮数: {args.rounds} | prompt: 「{args.prompt}」")

    # 登录拿 token
    print("\n登录中...")
    try:
        token = await login(args.host, args.port, args.user, args.password)
    except Exception as e:
        print(f"❌ 登录失败: {e}", file=sys.stderr)
        return 1
    print("✅ 登录成功")

    ws_url = f"ws://{args.host}:{args.port}/ws/chat?token={token}"
    results = []

    async with aiohttp.ClientSession() as session:
        for i in range(1, args.rounds + 1):
            try:
                result = await probe_one_round(session, ws_url, args.prompt)
                results.append(result)
                print_round_report(i, args.prompt, result)
            except Exception as e:
                print(f"\n❌ 第 {i} 轮失败: {type(e).__name__}: {e}", file=sys.stderr)
            # 轮间间隔，避免过于频繁
            if i < args.rounds:
                print(f"\n  ... 休息 3 秒后开始下一轮 ...")
                await asyncio.sleep(3)

    # 汇总
    print(f"\n{'#' * 70}")
    print("汇总")
    print(f"{'#' * 70}")
    spins = [r["spin_duration_ms"] for r in results if r["spin_duration_ms"] is not None]
    if spins:
        print(f"转圈时长(stream_start→首内容):")
        for i, s in enumerate(spins, 1):
            flag = " ⚠️" if s > 2000 else ""
            print(f"  第{i}轮: {s:.0f}ms{flag}")
        print(f"  平均: {sum(spins) / len(spins):.0f}ms  最大: {max(spins):.0f}ms")
    else:
        print("未捕获到 stream_start → 首 内容 的时序（可能事件缺失）")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
