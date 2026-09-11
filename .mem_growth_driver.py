"""多轮发消息驱动管道 + 内核进程内存采样（内存增长黑盒观测）。

用法:
    python .mem_growth_driver.py --rounds 6 --interval 5
    python .mem_growth_driver.py --rounds 3 --prompt "自定义消息"

复用 scripts/eval_bench/kernel_client.py 的登录/建会话/WS 派发；
内存采样读内核进程 Private Bytes（水位自愈同口径）+ WorkingSet + sidecar RSS。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "eval_bench"))
from kernel_client import KernelClient, _extract_pipeline_id  # noqa: E402
import websockets  # noqa: E402


async def dispatch_verbose(ws_url: str, thread_id: str, content: str, cmid: str, timeout_s: float = 600):
    """发送 user_input 并收流到终态；终态事件原文打印（含错误详情）。"""
    import json as _json

    terminal, pipeline_id = None, None
    async with websockets.connect(ws_url, open_timeout=15, max_size=64 * 1024 * 1024) as ws:
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        await ws.send(_json.dumps({
            "type": "user_input", "thread_id": thread_id, "content": content,
            "pipeline_id": "", "attachments": [], "enable_thinking": False,
            "thinking_strength": "", "client_message_id": cmid,
        }))
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                event = _json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or "")
            pid = _extract_pipeline_id(event)
            if pid and not pipeline_id:
                pipeline_id = pid
            if etype in ("stream_end", "stream_error", "error"):
                terminal = etype
                print("TERMINAL-EVENT:", _json.dumps(event, ensure_ascii=False)[:2000], flush=True)
                break
    return terminal, pipeline_id

BASE_URL = "http://127.0.0.1:9100"


def sample_memory(label: str) -> dict:
    """采样内核进程 Private/WorkingSet(MB) + 全部 python sidecar RSS 总和(MB)。"""
    import psutil

    kernel = None
    for p in psutil.process_iter(["name", "pid"]):
        if p.info["name"] == "agentos-kernel.exe":
            kernel = p
            break
    row: dict = {"label": label, "ts": datetime.now(timezone.utc).strftime("%H:%M:%S")}
    if kernel is None:
        row["kernel_pid"] = None
    else:
        mem = kernel.memory_full_info()
        row["kernel_pid"] = kernel.pid
        row["private_mb"] = round(mem.private / 1048576, 1)
        row["ws_mb"] = round(mem.rss / 1048576, 1)
        row["uss_mb"] = round(getattr(mem, "uss", 0) / 1048576, 1)
    sidecar_mb = 0.0
    sidecar_n = 0
    for p in psutil.process_iter(["name", "memory_info"]):
        if p.info["name"] == "python.exe":
            try:
                sidecar_mb += p.info["memory_info"].rss / 1048576
                sidecar_n += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    row["sidecar_mb"] = round(sidecar_mb, 1)
    row["sidecar_n"] = sidecar_n
    return row


def fmt_row(row: dict) -> str:
    return (
        f"[{row['ts']}] {row['label']:<14} "
        f"private={row.get('private_mb', '-'):>7}MB  ws={row.get('ws_mb', '-'):>7}MB  "
        f"uss={row.get('uss_mb', '-'):>7}MB  sidecar={row.get('sidecar_mb', '-'):>8}MB({row.get('sidecar_n')}py)"
    )


async def run_rounds(rounds: int, prompt_tpl: str, settle_s: float) -> None:
    password = os.environ.get("AGENTOS_ADMIN_PASSWORD")
    if not password:
        print("ERROR: 需要 AGENTOS_ADMIN_PASSWORD", file=sys.stderr)
        sys.exit(2)

    client = KernelClient(BASE_URL)
    client.login("admin", password)
    session = client.create_session(title="mem-growth-obs")
    thread_id = session["thread_id"]
    print(f"session={thread_id}")

    rows: list[dict] = []
    rows.append(sample_memory("baseline"))
    print(fmt_row(rows[-1]), flush=True)

    for i in range(1, rounds + 1):
        content = prompt_tpl.format(n=i)
        t0 = time.monotonic()
        terminal, pid = await dispatch_verbose(
            client.ws_chat_url(), thread_id, content, f"mem-obs-{i}", timeout_s=600
        )
        elapsed = time.monotonic() - t0
        # 终态后留 settle 秒让引擎收尾（事件落库/瞬时对象可回收）再采样
        time.sleep(settle_s)
        row = sample_memory(f"round-{i}(term={terminal},{elapsed:.0f}s)")
        rows.append(row)
        print(fmt_row(row), flush=True)
        if terminal not in ("stream_end",):
            print(f"  WARN: round {i} terminal={terminal} pipeline={pid}")

    print("\n=== 增长曲线（相对基线 MB）===")
    base = rows[0]
    for row in rows[1:]:
        if row.get("private_mb") is not None and base.get("private_mb") is not None:
            dp = round(row["private_mb"] - base["private_mb"], 1)
            dw = round(row["ws_mb"] - base["ws_mb"], 1)
            print(f"  {row['label']:<24} private_delta={dp:>+8}MB  ws_delta={dw:>+8}MB")
    with open(".mem_growth_obs.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("samples -> .mem_growth_obs.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--interval", type=float, default=5.0, help="轮间额外间隔秒")
    ap.add_argument("--settle", type=float, default=8.0, help="终态到采样前的沉降秒")
    ap.add_argument("--prompt", default="请用一句话回答：1+{n}等于几？不用调用任何工具，直接回答。")
    args = ap.parse_args()
    asyncio.run(run_rounds(args.rounds, args.prompt, args.settle))


if __name__ == "__main__":
    main()
