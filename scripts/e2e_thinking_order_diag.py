"""E2E 思考顺序诊断：捕获真实 WS 事件，重点打印 sequence。

目标：定位"思考过程渲染顺序错乱"的根因。
- 同时存在多段思考（中文，文本内 think 标签）与原生 reasoning（英文）
- 与工具调用 / 文本回复交错

关键诊断点（决定根因）：
  同一 message_id 内，多次 thinking_start / stream_chunk 的 sequence
  - 若单调递增（1,2,3...）→ 后端 sequence 正确，根因在前端
  - 若重置/冲突（多次 thinking 都是 1）→ 后端 _part_seq 在迭代间被重置，根因在后端

输出：按到达顺序打印 [idx] type seq=<n> content=<前缀>，并在末尾汇总。
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
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "{}")


def _seq(evt: dict) -> str:
    """提取事件的 part sequence（后端放在 data.sequence 下）+ message_id。"""
    d = evt.get("data", {}) if isinstance(evt.get("data"), dict) else {}
    s = d.get("sequence")
    mid = str(d.get("message_id", ""))[:6]
    seq_part = f"seq={s}" if s is not None else "seq=—"
    return f"{seq_part:12s} msg={mid}"


def _content(evt: dict) -> str:
    d = evt.get("data", {}) if isinstance(evt.get("data"), dict) else {}
    t = evt.get("type", "?")
    if t in ("stream_chunk", "thinking_chunk"):
        return f"content={str(d.get('content', ''))[:30]!r}"
    if t == "thinking_start":
        return "(thinking_start)"
    if t == "tool_start":
        return f"tool={d.get('tool_name', '')}"
    if t == "tool_result":
        return f"tool={d.get('tool_name', '')} dur={d.get('duration_ms')}"
    if t == "stream_start":
        return f"msg={str(d.get('message_id', ''))[:8]}"
    if t in ("stream_end", "new_message"):
        parts = d.get("parts", []) or []
        order = " → ".join(f"{p.get('type')}(seq={p.get('sequence')})" for p in parts)
        return f"parts=[{order}]"
    if t == "state_change":
        return f"status={d.get('status', '')}"
    return ""


async def main() -> None:
    print("=" * 64)
    print("E2E 思考顺序诊断（enable_thinking=True）")
    print("=" * 64)

    code, data = http("/api/v1/auth/login", "POST", body={"username": "admin", "password": "admin123"})
    assert code == 200, f"登录失败: {code}"
    token = data["access_token"]
    print("[OK] 登录成功")

    code, data = http("/api/v1/threads", "POST", token=token,
                      body={"title": "E2E-思考顺序", "intent": "诊断思考渲染顺序"})
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
    events: list[dict] = []
    print(f"\n[WS] 连接 ...\n")

    async with websockets.connect(uri, max_size=2 ** 20, ping_interval=30) as ws:
        # 排空 connection_confirmation
        try:
            raw0 = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"[WS] 首帧: {json.loads(raw0).get('type')}")
        except asyncio.TimeoutError:
            print("[WS] [WARN] 未收到首帧")

        # 关键：enable_thinking=True 触发思考；prompt 要求"先思考再调工具"，制造 多段思考+工具 交错
        user_msg = {
            "type": "user_input",
            "thread_id": thread_id,
            "content": "请先思考一下，然后用 file_read 读取 /tmp/thinking_order_check.txt 的内容，"
                       "再简短告诉我结果。",
            "pipeline_id": pipeline_id,
            "attachments": [],
            "enable_thinking": True,
            "client_message_id": f"thinkdiag-{int(time.time() * 1000)}",
        }
        print(f"[WS] 发送 user_input（enable_thinking=True）\n")
        await ws.send(json.dumps(user_msg))

        deadline = time.time() + 90
        last_evt = time.time()
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                if time.time() - last_evt > 8:
                    break
                try:
                    await ws.send(json.dumps({"type": "heartbeat", "thread_id": ""}))
                except Exception:
                    pass
                continue
            except websockets.ConnectionClosed as cc:
                print(f"[WS] CLOSED code={cc.code} reason={cc.reason!r}")
                break

            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "?")
            if etype == "heartbeat_ack":
                continue
            events.append(evt)
            last_evt = time.time()
            idx = len(events)
            print(f"  [#{idx:02d}] {etype:16s} {_seq(evt):10s} {_content(evt)}")

            if etype == "state_change":
                st = (evt.get("data") or {}).get("status", "")
                if st in ("suspended", "completed", "error", "idle", "stopped"):
                    break

    # ── 汇总分析 ──
    print("\n" + "=" * 64)
    print("思考顺序分析（按 message_id 分组）")
    print("=" * 64)

    # 按 message_id 分组收集
    from collections import defaultdict
    groups: dict[str, dict] = defaultdict(lambda: {
        "thinking_start": [], "thinking_chunk": [], "stream_chunk": [],
        "tool_start": [], "tool_result": [],
    })
    for e in events:
        t = e.get("type", "")
        if t not in ("thinking_start", "thinking_chunk", "stream_chunk", "tool_start", "tool_result"):
            continue
        d = e.get("data", {}) if isinstance(e.get("data"), dict) else {}
        mid = str(d.get("message_id", ""))[:8] or "<no-mid>"
        s = d.get("sequence")
        if isinstance(s, int):
            groups[mid][t].append(s)

    print(f"  涉及 message_id 数: {len(groups)}")
    for mid, g in groups.items():
        print(f"\n  ── 消息 {mid} ──")
        for t in ("thinking_start", "thinking_chunk", "stream_chunk", "tool_start", "tool_result"):
            seqs = g[t]
            if not seqs:
                continue
            lo, hi = min(seqs), max(seqs)
            monotonic = all(seqs[i] <= seqs[i + 1] for i in range(len(seqs) - 1))
            flag = "✅单调" if monotonic else "❌非单调(乱序!)"
            print(f"    {t:16s} count={len(seqs):4d} range=[{lo}, {hi}] {flag}")
            if not monotonic:
                # 找出所有回退点
                drops = [(i, seqs[i], seqs[i + 1]) for i in range(len(seqs) - 1) if seqs[i] > seqs[i + 1]]
                for i, a, b in drops[:5]:
                    print(f"        回退点 #{i}: {a} → {b}")

    # 跨类型顺序判定：thinking 的 max 是否 < text 的 min（思考应在正文前）
    print("\n  ── 跨类型顺序（思考 vs 正文，按到达顺序而非数值）──")
    for mid, g in groups.items():
        t_starts = g["thinking_start"]
        s_chunks = g["stream_chunk"]
        if t_starts and s_chunks:
            print(f"  消息 {mid}: thinking_start seqs={t_starts} 正文首chunk={s_chunks[0] if s_chunks else None}")
            if max(t_starts) > s_chunks[0]:
                print(f"    ❌ 思考 sequence({max(t_starts)}) > 正文({s_chunks[0]}) → 思考被排到正文之后！")

    # ── 块切换序列（金标准判定）：按 message_id 分组，每组内按到达顺序记录 part 块 ──
    print("\n  ── 按 message_id 分组的 part 块序列（金标准）──")
    # 先按到达顺序对每个 message_id 收集块切换点
    per_msg_blocks: dict[str, list[tuple[str, int]]] = {}
    per_msg_last_type: dict[str, str | None] = {}
    for e in events:
        t = e.get("type", "")
        if t not in ("thinking_start", "stream_chunk", "tool_start"):
            continue
        d = e.get("data", {}) if isinstance(e.get("data"), dict) else {}
        s = d.get("sequence")
        if not isinstance(s, int):
            continue
        mid = str(d.get("message_id", ""))[:8] or "<no-mid>"
        last = per_msg_last_type.get(mid)
        if t != last:
            per_msg_blocks.setdefault(mid, []).append((t, s))
            per_msg_last_type[mid] = t

    if not per_msg_blocks:
        print("    （本轮未产生 thinking/text/tool 事件）")
    all_ok = True
    for mid, blocks in per_msg_blocks.items():
        print(f"    消息 {mid} → " + " | ".join(f"{t}({s})" for t, s in blocks))
        seqs_only = [s for _, s in blocks]
        monotonic = all(seqs_only[i] < seqs_only[i + 1] for i in range(len(seqs_only) - 1))
        ok_str = "✅ 严格递增" if monotonic else "❌ 非单调"
        if not monotonic:
            all_ok = False
        print(f"      [{ok_str}] 块级 sequence {'反映真实到达顺序' if monotonic else '顺序仍会错乱'}")
    print(f"\n  [总结] {'✅ 所有消息块级 sequence 正确' if all_ok and per_msg_blocks else '❌ 存在顺序问题'}")

    # stream_end 的 parts 顺序（后端最终构建的顺序）
    for e in events:
        if e.get("type") in ("stream_end", "new_message"):
            parts = (e.get("data") or {}).get("parts", []) or []
            if parts:
                print(f"\n  后端 stream_end 最终 parts 顺序:")
                for p in parts:
                    print(f"    - {p.get('type')}(seq={p.get('sequence')}) "
                          f"content={str(p.get('content', ''))[:24]!r}")
            break

    print("\n诊断完成")


if __name__ == "__main__":
    asyncio.run(main())
