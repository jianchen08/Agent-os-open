# @feature: WS 多端扇出验收探针（同 user 双真连接 + 晚注册连接） | @ci: 手动工具
"""验收口径（ADR 2026-10-01 多前端连接方案 A）：

① 同 user 两条真连接，A 发 active_thread_changed + user_input 后，A、B 都收到
   stream 类帧（stream_chunk/text_delta 与 stream_end）；
② B 晚于 user_input 注册（派发受理后建连，不发任何帧）也收到其注册之后的
   后续 stream 帧。

提取契约与前端 eventPayload 同源：载荷在帧的 ``data`` 子对象（顶层双取兼容）。
``text_delta``/``stream_chunk`` 的文本在 ``data.text``/``data.content``——顶层
取值恒空（2026-10-02 扇出回归排查实锤：帧一直送达，坏探针顶层取值伪报零收到）。

用法: python scripts/ws_multi_conn_probe.py [--base http://127.0.0.1:9101]
前置: 内核在线；AGENTOS_ADMIN_PASSWORD 环境变量或 %APPDATA%/agentos/.env 可读。
退出码: 全部断言通过 0，否则 1。
"""

import argparse
import asyncio
import io
import json
import os
import sys
import time
import urllib.request

import websockets

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

STREAM_EVENTS = ("text_delta", "stream_chunk")


def http(base, method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def login_admin(base):
    password = os.environ.get("AGENTOS_ADMIN_PASSWORD", "")
    if not password:
        env_path = os.path.join(os.environ.get("APPDATA", ""), "agentos", ".env")
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("AGENTOS_ADMIN_PASSWORD="):
                    password = line.split("=", 1)[1].strip()
    return http(base, "POST", "/api/v1/auth/login", None, {"username": "admin", "password": password})["access_token"]


def ws_url(base, token):
    ticket = http(base, "POST", "/api/v1/ws-ticket", token, {})["ticket"]
    return f"{base.replace('http', 'ws', 1)}/ws/chat?ticket={ticket}&version=3.0.0"


async def reader(ws, label, sink, stop):
    while not stop.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
        except TimeoutError:
            continue
        except Exception as exc:  # 连接异常终止如实记录
            sink[label]["types"].append(f"<closed:{type(exc).__name__}>")
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = event.get("type") or ""
        sink[label]["types"].append(etype)
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        if etype in STREAM_EVENTS:
            text = data.get("text") or data.get("delta") or data.get("content") or ""
            if isinstance(text, str):
                sink[label]["text"] += text


def has_received(sink, label, etypes):
    return any(t in sink[label]["types"] for t in etypes)


async def run(base):
    token = login_admin(base)
    session = http(
        base,
        "POST",
        "/api/v1/sessions",
        token,
        {"title": "多端扇出验收", "intent": "ws_multi_conn_probe", "metadata": {"isolation_mode": "isolated"}},
    )
    tid = session["thread_id"]
    pid = session.get("active_pipeline_id", "")
    print("probe thread:", tid, "pid:", pid)

    sink = {k: {"types": [], "text": ""} for k in ("A", "B")}
    stop = asyncio.Event()
    async with websockets.connect(ws_url(base, token), max_size=2**24) as ws_a:
        task_a = asyncio.create_task(reader(ws_a, "A", sink, stop))
        await asyncio.sleep(0.5)
        await ws_a.send(json.dumps({"type": "active_thread_changed", "thread_id": tid, "pipeline_id": pid}))
        await ws_a.send(
            json.dumps(
                {
                    "type": "user_input",
                    "thread_id": tid,
                    "content": "只回答两个字:正常",
                    "pipeline_id": pid,
                    "attachments": [],
                    "enable_thinking": False,
                    "thinking_strength": "",
                    "client_message_id": f"mc-{int(time.time())}",
                    "execution_context": {"isolation": {"level": "isolated"}},
                },
                ensure_ascii=False,
            )
        )
        # ② B 晚于 user_input 注册：派发受理后再建连，不发送任何帧
        await asyncio.sleep(2)
        ws_b = await websockets.connect(ws_url(base, token), max_size=2**24)
        task_b = asyncio.create_task(reader(ws_b, "B", sink, stop))
        deadline = time.time() + 90
        try:
            while time.time() < deadline:
                await asyncio.sleep(2)
                both_done = all("pipeline_round_finished" in sink[label]["types"] for label in ("A", "B"))
                errored = any("stream_error" in sink[label]["types"] for label in ("A", "B"))
                if both_done or errored:
                    await asyncio.sleep(2)
                    break
        finally:
            stop.set()
            await asyncio.sleep(3)
            task_a.cancel()
            task_b.cancel()
            await ws_b.close()

    for label in ("A", "B"):
        print(
            f"--- conn {label}: frames={len(sink[label]['types'])} "
            f"reply-len={len(sink[label]['text'])} head={sink[label]['text'][:60]!r}"
        )

    ok_a = (
        "user_input_ack" in sink["A"]["types"]
        and has_received(sink, "A", STREAM_EVENTS)
        and "stream_end" in sink["A"]["types"]
    )
    ok_b = has_received(sink, "B", STREAM_EVENTS) and "stream_end" in sink["B"]["types"]
    print("ASSERT-A(ack+stream+end):", ok_a, "| ASSERT-B(late-join stream+end):", ok_b)
    return ok_a and ok_b


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:9101")
    args = parser.parse_args()
    passed = asyncio.run(run(args.base))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
