#!/usr/bin/env python3
"""M1 触发器端到端联调 v2（e2e 专用，完成后可删）。

链路：登录 → 建会话线程 → 注册 T2（永假条件，仅作为 state 写源）→ 读 T2
注册表键的精确序列化值 → 注册 T1（条件 `K2 != '<v1>'`，种子时相等=false）→
手动点火 T2（fire_count 变 → K2 值变 → store 锚点求值 → T1 边沿命中）→
内核 trigger.fired → 插件注入 → ack → 轮询 T1 fire_count ≥ 1。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("AGENTOS_KERNEL_URL", "http://127.0.0.1:9100")
PASSWORD = os.environ["AGENTOS_ADMIN_PASSWORD"]


def _request(path: str, payload: dict | None, token: str | None = None, method: str = "POST") -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Main-Agent-Request": "true"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data if method == "POST" else None,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _login() -> str:
    return _request("/api/v1/auth/login", {"username": "admin", "password": PASSWORD})["access_token"]


def main() -> None:
    token = _login()
    print("[1] login ok")

    thread = _request("/api/v1/sessions", {"title": "M1-e2e-v5", "intent": "e2e"}, token)
    thread_id = thread.get("thread_id") or (thread.get("data") or {}).get("thread_id")
    assert thread_id, f"建线程响应无 thread_id: {json.dumps(thread)[:200]}"
    print(f"[2] thread_id={thread_id}")

    # 派发最小任务 → 产生真实 12-hex 管道（chat.send_message 契约要求 12-hex）。
    ticket = _request("/api/v1/ws-ticket", {}, token)["ticket"]
    from websockets.sync.client import connect

    ws_url = BASE.replace("http", "ws", 1) + f"/ws/chat?ticket={ticket}"
    msg = {
        "type": "user_input",
        "thread_id": thread_id,
        "content": "e2e 冒烟：只回复 ok。",
        "pipeline_id": "",
        "attachments": [],
        "enable_thinking": False,
        "thinking_strength": "",
        "client_message_id": f"e2e-m1-{int(time.time() * 1000)}",
    }
    with connect(ws_url, open_timeout=15) as ws:
        ws.send(json.dumps(msg, ensure_ascii=False))
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                raw = ws.recv(timeout=max(1.0, deadline - time.time()))
            except TimeoutError:
                break
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if frame.get("type") in ("stream_end", "stream_error", "error"):
                break
    print("[2b] 任务已派发（LLM 失败不影响管道 id 产生）")
    time.sleep(2)

    # 从聚合读面解析该会话的 12-hex 管道 id。管道创建与 session_id 落读面
    # 有先后（真实 LLM 轮次下管道先以 running 出现），轮询等待而非单查。
    pipe = None
    deadline = time.time() + 30
    while time.time() < deadline:
        state = _request("/api/v1/pipelines/state", None, token, method="GET")
        items = state.get("items") or (state.get("data") or {}).get("items") or []
        pipe = next(
            (
                r.get("pipeline_id")
                for r in items
                if (r.get("state") or {}).get("session_id") == thread_id
            ),
            None,
        )
        if pipe:
            break
        time.sleep(2)
    assert pipe, f"聚合读面 30s 内未找到会话对应管道: {json.dumps(state)[:300]}"
    print(f"[2c] pipeline_id={pipe}")

    t2 = _request("/ext/trigger_setup_tool/triggers", {
        "trigger_type": "condition",
        "condition": "1 == 2",
        "pipeline_id": pipe,
        "message": "[e2e] T2",
        "name": "M1-e2e-T2",
    }, token)
    t2body = t2.get("data") if isinstance(t2.get("data"), dict) else t2
    t2id = (t2body.get("trigger") or {}).get("trigger_id") or t2body.get("trigger_id")
    assert t2id, f"T2 注册失败: {json.dumps(t2)[:300]}"
    print(f"[3] T2 registered: {t2id}")
    time.sleep(1)

    k2key = f"task.trigger.registry.{t2id}"

    import sqlite3
    db = os.environ.get("AGENTOS_E2E_DB", "C:/Users/Administrator/AppData/Local/Temp/agentos-e2e/trigger.db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT value FROM pipeline_state WHERE pipeline_id=? AND field_key=?",
        (pipe, f"task.trigger.registry.{t2id}"),
    ).fetchone()
    assert row, f"DB 无 {k2key if False else ''}K2 行"
    v1 = row[0]
    assert isinstance(v1, str) and "'" not in v1, f"K2 值不可用作条件字面量: {v1[:120]}"
    print(f"[4] K2 value read (len={len(v1)})")

    t1 = _request("/ext/trigger_setup_tool/triggers", {
        "trigger_type": "condition",
        # 读面会把 JSON 文本解码回对象——下钻字段比较（勿与原始序列化串比较，
        # 对象 != 字符串恒真会让种子=true、边沿死锁）。
        "condition": f"{k2key}.fire_count != 0",
        "pipeline_id": pipe,
        "message": "[M1-e2e] T1 FIRED",
        "name": "M1-e2e-T1",
    }, token)
    t1body = t1.get("data") if isinstance(t1.get("data"), dict) else t1
    t1id = (t1body.get("trigger") or {}).get("trigger_id") or t1body.get("trigger_id")
    assert t1id, f"T1 注册失败: {json.dumps(t1)[:300]}"
    print(f"[5] T1 registered: {t1id} (armed on K2 change)")

    fired_manual = _request(f"/ext/trigger_setup_tool/triggers/{t2id}/trigger", {}, token)
    print(f"[6] manual fire T2: {json.dumps(fired_manual)[:150]}")

    ok = False
    detail: dict = {}
    for _ in range(20):
        time.sleep(3)
        try:
            token = _login()  # token 过期自愈
            q = _request(f"/ext/trigger_setup_tool/triggers/{t1id}", None, token, method="GET")
        except urllib.error.HTTPError:
            continue
        d = q.get("data") if isinstance(q.get("data"), dict) else q
        trig = d.get("trigger") or d
        detail = trig
        fc = trig.get("fire_count") or 0
        if isinstance(fc, int) and fc >= 1:
            ok = True
            break
    print(f"[7] T1 fired={ok} fire_count={detail.get('fire_count')} status={detail.get('status')}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
