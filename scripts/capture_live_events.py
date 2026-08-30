# @feature: 聊天流式事件抓取（真机，供前端重放复现） | @ci: 手动工具
"""真机抓取一次多轮工具循环的完整 WS 事件流 → frontend fixture。

用法: python scripts/capture_live_events.py
前置: 内核 :9100 在线且已登录身份可用（E2E_PARITY_USERNAME 或 admin）。
"""

import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests", "e2e_02"))

from e2e_helpers import KERNEL_URL, create_session, http_post_json, login_admin, ws_chat_url

PROMPT = "请分别用 file_read 读取 config/agents/main/agentos.yaml 和 config/pipelines/autonomous.yaml 这两个文件（先读第一个再读第二个），最后把两个文件的内容概要告诉我。"
OUT = os.path.join(
    os.path.dirname(__file__), "..", "frontend", "src", "stores", "__tests__", "__fixtures__", "live_events.json"
)


def _token():
    user = os.environ.get("E2E_PARITY_USERNAME")
    if user:
        status, body, _ = http_post_json(
            f"{KERNEL_URL}/api/v1/auth/login",
            {"username": user, "password": os.environ.get("E2E_PARITY_PASSWORD", "parity12345")},
            timeout=10,
        )
        if status == 200 and body.get("access_token"):
            return body["access_token"], user
    return login_admin(), "admin"


async def main():
    token, user = _token()
    session = create_session(token, title="live-capture")
    sid = session["thread_id"]
    url = ws_chat_url(token)
    import websockets

    events = []
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except TimeoutError:
            pass
        await ws.send(
            json.dumps(
                {
                    "type": "user_input",
                    "thread_id": sid,
                    "content": PROMPT,
                    "pipeline_id": "",
                    "attachments": [],
                    "enable_thinking": True,
                    "thinking_strength": "medium",
                    "client_message_id": f"e2e-capture-{uuid.uuid4().hex[:8]}",
                }
            )
        )
        last = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - last < 45:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except TimeoutError:
                continue
            last = asyncio.get_running_loop().time()
            data = json.loads(raw)
            events.append(data)
            t = data.get("type")
            if t in ("stream_end", "new_message", "tool_result", "finish", "stream_error"):
                print(f"  [{t}]")
            if t == "stream_error":
                break
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=1)
    types = [e.get("type") for e in events]
    print("captured:", len(events), "events |", {t: types.count(t) for t in set(types)})
    # 删除会话（避免残留）
    try:
        from e2e_helpers import delete_session

        delete_session(token, sid)
    except Exception as e:
        print("cleanup:", e)


asyncio.run(main())
