"""E2E 多轮顺序诊断：捕获后端两轮对话的真实事件推送序列。

目标：验证后端在「文本→工具→文本」单轮、以及连发两轮时的 WS 事件顺序。
重点观察：
1. 每轮的 message_id 是否变化（每轮一个 AI 消息 vs 复用）
2. tool_start/tool_result 相对 stream_chunk 的位置
3. stream_end / state_change 的时序
4. 第二轮 user_input 是否触发新的 stream_start（新 message_id）

输出：按到达顺序打印每个事件的 [seq] type + 关键字段，便于人工核对顺序。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
import urllib.error

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


def _brief(data: dict) -> str:
    """事件关键字段摘要（一眼看出顺序语义）。"""
    t = data.get("type", "?")
    d = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
    if t in ("stream_start",):
        return f"msg={d.get('message_id', '')[:8]} pid={d.get('pipeline_id', '')[:8]} tid={d.get('_threadId', '')[:8]}"
    if t == "stream_chunk":
        c = d.get("content", "")
        return f"msg={d.get('message_id', '')[:8]} content={c[:24]!r}"
    if t == "thinking_chunk":
        return f"content={d.get('content', '')[:24]!r}"
    if t == "thinking_start":
        return ""
    if t == "tool_start":
        return f"tool={d.get('tool_name', '')[:16]} call_id={d.get('call_id', '')[:12]} msg={d.get('message_id', '')[:8]}"
    if t == "tool_result":
        return f"tool={d.get('tool_name', '')[:16]} call_id={d.get('call_id', '')[:12]} dur={d.get('duration_ms')}"
    if t in ("stream_end", "new_message"):
        return f"msg={d.get('message_id', '')[:8]} parts={len(d.get('parts', []))}"
    if t == "state_change":
        return f"status={d.get('status', '')[:16]} msg={d.get('message_id', '')[:8]}"
    if t == "iteration":
        return f"iter={d.get('iteration', '')}/{d.get('max_iterations', '')}"
    if t in ("stream_error", "error"):
        return f"err={str(d)[:60]}"
    return ""


async def drain_until_idle(ws, tag: str, deadline_s: float = 70) -> list[dict]:
    """收集事件直到本轮结束（state_change suspended/completed 或超时）。

    返回事件列表（含 type/data）。每行打印：[序号] type 摘要。
    """
    events: list[dict] = []
    deadline = time.time() + deadline_s
    last_event_time = time.time()
    idle_break = False

    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
        except asyncio.TimeoutError:
            # 8s 无事件：若距上一事件已超过 6s，认为本轮 idle
            if time.time() - last_event_time > 6:
                idle_break = True
                break
            # 否则发心跳探活后继续
            try:
                await ws.send(json.dumps({"type": "heartbeat", "thread_id": ""}))
            except Exception:
                pass
            continue
        except websockets.ConnectionClosed as cc:
            print(f"[{tag}] WS CLOSED code={cc.code} reason={cc.reason!r}")
            break

        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        events.append(evt)
        last_event_time = time.time()
        etype = evt.get("type", "?")
        data = evt.get("data", {})
        idx = len(events)
        print(f"  [{tag} #{idx:02d}] {etype:18s} {_brief(evt)}")

        # state_change 的 suspended/completed 视为本轮终止
        if etype == "state_change":
            st = data.get("status", "") if isinstance(data, dict) else ""
            if st in ("suspended", "completed", "error", "idle", "stopped"):
                break

    if idle_break:
        print(f"  [{tag}] 本轮 idle 结束（{int(time.time() - (deadline - deadline_s))}s 内 {len(events)} 事件）")
    return events


async def send_and_collect(ws, thread_id: str, pipeline_id: str, content: str, tag: str) -> list[dict]:
    """发一条 user_input 并收集本轮事件。"""
    msg = {
        "type": "user_input",
        "thread_id": thread_id,
        "content": content,
        "pipeline_id": pipeline_id,
        "attachments": [],
        "enable_thinking": False,
        "client_message_id": f"{tag}-{int(time.time()*1000)}",
    }
    print(f"\n{'='*60}\n[{tag}] 发送: {content!r}\n{'='*60}")
    await ws.send(json.dumps(msg))
    return await drain_until_idle(ws, tag)


async def main() -> None:
    print("=" * 60)
    print("E2E 多轮顺序诊断（后端 WS 推送序列）")
    print("=" * 60)

    code, data = http("/api/v1/auth/login", "POST", body={"username": "admin", "password": "admin123"})
    assert code == 200, f"登录失败: {code}"
    token = data["access_token"]
    print("[OK] 登录成功")

    code, data = http("/api/v1/threads", "POST", token=token,
                      body={"title": "E2E-多轮顺序", "intent": "验证多轮事件顺序"})
    assert code in (200, 201), f"创建会话失败: {code} {data}"
    thread_id = data["thread_id"]
    pipeline_id = (data.get("pipeline_ids") or [None])[0]
    if not pipeline_id:
        for _ in range(10):
            time.sleep(1)
            _, d = http(f"/api/v1/threads/{thread_id}", token=token)
            pids = d.get("pipeline_ids") or []
            if pids:
                pipeline_id = pids[0]
                break
    assert pipeline_id, "主管道未就绪"
    print(f"[OK] 会话={thread_id} 管道={pipeline_id}")

    uri = f"{WS_BASE}/ws/chat?token={token}"
    print(f"\n[WS] 连接 {WS_BASE}/ws/chat?token=...")

    async with websockets.connect(uri, max_size=2**20, ping_interval=30) as ws:
        # 排空 connection_confirmation
        try:
            raw0 = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"[WS] 首帧: {json.loads(raw0).get('type')}")
        except asyncio.TimeoutError:
            print("[WS] [WARN] 未收到 connection_confirmation")

        # 第一轮：触发工具调用（读取一个不存在文件 → file_read）
        events1 = await send_and_collect(
            ws, thread_id, pipeline_id,
            "请用 file_read 读取 /tmp/order_test_1.txt 的内容",
            "第1轮",
        )

        # 第二轮：再发一条，看是否产生新的 stream_start / 新 message_id
        events2 = await send_and_collect(
            ws, thread_id, pipeline_id,
            "再用 file_read 读取 /tmp/order_test_2.txt 的内容",
            "第2轮",
        )

    # ── 汇总分析 ──
    print("\n" + "=" * 60)
    print("顺序分析")
    print("=" * 60)

    def analyze(events: list[dict], tag: str) -> None:
        print(f"\n--- {tag} ({len(events)} 事件) ---")
        types = [e.get("type", "?") for e in events]
        # 提取所有出现的 message_id
        msg_ids = set()
        for e in events:
            d = e.get("data", {}) if isinstance(e.get("data"), dict) else {}
            for k in ("message_id", "_threadId"):
                v = d.get(k) or e.get(k)
                if v:
                    msg_ids.add(str(v)[:12])
        print(f"  事件类型序列: {types}")
        print(f"  涉及 message_id: {msg_ids}")

        # 关键顺序断言
        def idx_of(t: str) -> int:
            return types.index(t) if t in types else -1
        s_start = idx_of("stream_start")
        tool_start = idx_of("tool_start")
        tool_result = idx_of("tool_result")
        s_end = idx_of("stream_end") if "stream_end" in types else idx_of("state_change")

        checks = []
        if s_start >= 0:
            checks.append(("stream_start 在最前", s_start == 0))
        if tool_start >= 0 and tool_result >= 0:
            checks.append(("tool_start 早于 tool_result", tool_start < tool_result))
        if s_start >= 0 and tool_start >= 0:
            checks.append(("stream_start 早于 tool_start", s_start < tool_start))
        if tool_result >= 0 and s_end >= 0:
            checks.append(("tool_result 早于 终态", tool_result < s_end))
        for desc, ok in checks:
            print(f"    [{'OK' if ok else 'FAIL'}] {desc}")

    analyze(events1, "第1轮")
    analyze(events2, "第2轮")

    # 跨轮：第2轮必须有新的 stream_start（证明是新一轮而非续写）
    t1_starts = [e for e in events1 if e.get("type") == "stream_start"]
    t2_starts = [e for e in events2 if e.get("type") == "stream_start"]
    print(f"\n--- 跨轮 ---")
    print(f"  第1轮 stream_start 数: {len(t1_starts)}")
    print(f"  第2轮 stream_start 数: {len(t2_starts)}")
    if t1_starts and t2_starts:
        mid1 = (t1_starts[0].get("data", {}) or {}).get("message_id", "")
        mid2 = (t2_starts[0].get("data", {}) or {}).get("message_id", "")
        same = mid1 == mid2
        print(f"  第1轮首 message_id: {mid1[:12]}")
        print(f"  第2轮首 message_id: {mid2[:12]}")
        print(f"    [{'OK' if not same else 'WARN'}] 两轮 message_id {'相同(复用)' if same else '不同(各自独立)'}")

    print("\n诊断完成")


if __name__ == "__main__":
    asyncio.run(main())
