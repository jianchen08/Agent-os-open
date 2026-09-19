"""R154：WS 普通聊天冒烟（内核 58ab83965 后）——新会话发消息收回复。"""
import asyncio
import io
import json
import sys
import uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, "scripts/eval_bench")
from kernel_client import dispatch_and_collect  # noqa: E402
import urllib.request  # noqa: E402


def _ticket() -> str:
    login = urllib.request.Request(
        "http://127.0.0.1:9100/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin12345"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    tok = json.loads(urllib.request.urlopen(login, timeout=10).read())["access_token"]
    req = urllib.request.Request(
        "http://127.0.0.1:9100/api/v1/ws-ticket",
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read()).get("ticket", "")


async def main() -> None:
    tid = open(".zctmp/r154_tid.txt").read().strip()
    ticket = _ticket()
    r = await dispatch_and_collect(
        f"ws://127.0.0.1:9100/ws/chat?ticket={ticket}",
        tid,
        "R154 冒烟：只回复两个字：收到",
        f"r154-{uuid.uuid4().hex[:8]}",
        timeout_s=150,
    )
    out = {
        "thread": tid,
        "terminal": getattr(r, "terminal", None),
        "reply": (getattr(r, "reply_text", "") or getattr(r, "text", "") or "")[:120],
        "pipeline": getattr(r, "pipeline_id", None),
    }
    print(json.dumps(out, ensure_ascii=False))


asyncio.run(main())
