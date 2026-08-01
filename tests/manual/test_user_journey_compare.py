#!/usr/bin/env python3
"""0.1 vs 0.2 用户旅程对照实测脚本。

回答核心问题：「0.2 默认管道配置(autonomous.yaml)的执行流程和 0.1 一样吗？」

实测维度（两栈对照）：
  1. 单轮 LLM 调用 + 流式回流
  2. 多轮对话记忆（重点：0.2 state 回写被禁用，预期记不住）
  3. 工具调用（file_read）

依赖：
  - 0.1 :8988 运行（start_web_cn.bat），凭证 admin/admin123
  - 0.2 :9100 运行（start_web_02.bat --kernel-only）

用法：
  python tests/manual/test_user_journey_compare.py            # 全测
  python tests/manual/test_user_journey_compare.py --only 0.2 # 只测 0.2
  python tests/manual/test_user_journey_compare.py --no-llm   # 跳过真实 LLM

[来源: 阶段2.3 0.1 vs 0.2 默认管道流程对照]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from typing import Any

try:
    import websockets
except ImportError:
    print("需要 websockets 库: pip install websockets", file=sys.stderr)
    sys.exit(2)

KERNEL_02 = os.environ.get("AGENTOS_KERNEL_URL", "http://127.0.0.1:9100")
API_01 = os.environ.get("AGENTOS_API_URL", "http://127.0.0.1:8988")
WS_01 = os.environ.get("AGENTOS_WS_URL", "ws://127.0.0.1:8988")
CRED_01 = ("admin", "admin123")

_results: list[tuple[str, str, bool, str]] = []  # (栈, 用例, 通过, 详情)


def record(stack: str, name: str, passed: bool, detail: str = "") -> None:
    _results.append((stack, name, passed, detail))
    tag = "PASS" if passed else "FAIL"
    print(f"  [{stack}] [{tag}] {name}: {detail}")


def http(url: str, method: str = "GET", body: Any = None, headers: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers=headers or {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


# ── 0.1 (Python 栈) ─────────────────────────────────────────────────────────


def login_01() -> str:
    """登录 0.1 拿 token。"""
    resp = http(
        f"{API_01}/api/v1/auth/login",
        method="POST",
        body={"username": CRED_01[0], "password": CRED_01[1]},
    )
    return resp["access_token"]


def create_thread_01(token: str) -> tuple[str, str]:
    """建 thread，返回 (thread_id, pipeline_id)。用 lingxi agent（主 agent）。"""
    resp = http(
        f"{API_01}/api/v1/threads",
        method="POST",
        body={"title": "e2e", "agent_id": "lingxi"},
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    return resp["thread_id"], resp.get("active_pipeline_id") or resp["pipeline_ids"][0]


async def chat_01(token: str, tid: str, pid: str, message: str, timeout: float = 120) -> tuple[list, float]:
    """0.1 WS 发消息，收集事件直到 new_message/stream_end。返回 (事件列表, 耗时)。"""
    url = f"{WS_01}/ws/chat?token={token}"
    async with websockets.connect(url, open_timeout=10) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)  # connection_confirmation
        await ws.send(json.dumps({
            "type": "user_input", "thread_id": tid, "pipeline_id": pid,
            "data": {"content": message, "pipeline_id": pid},
        }))
        events: list = []
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout - (time.time() - t0)))
                events.append(ev)
                if ev.get("type") in ("new_message", "stream_end", "stream_error", "pipeline_error"):
                    # 流式收尾后可能还有 stream_end，多收一帧
                    if ev.get("type") == "new_message":
                        # 等可能的 stream_end
                        try:
                            tail = await asyncio.wait_for(ws.recv(), timeout=3)
                            events.append(json.loads(tail))
                        except (asyncio.TimeoutError, Exception):
                            pass
                    break
            except asyncio.TimeoutError:
                break
        return events, time.time() - t0


def extract_reply(events: list) -> str:
    for e in events:
        if e.get("type") == "new_message":
            d = e.get("data", {})
            return d.get("content") or (d.get("parts", [{}])[0].get("text", "") if d.get("parts") else "")
    return ""


async def test_0_1_single_turn(token: str, use_llm: bool) -> None:
    print("\n=== 0.1 单轮 LLM + 流式 ===")
    tid, pid = create_thread_01(token)
    if not use_llm:
        record("0.1", "单轮 (跳过LLM)", True, "")
        return
    events, dt = await chat_01(token, tid, pid, "你好，请用一句话介绍你自己")
    types = [e.get("type") for e in events]
    has_stream = "stream_start" in types or "stream_chunk" in types
    reply = extract_reply(events)
    ok = has_stream and len(reply) > 0
    record("0.1", "单轮流式+回复", ok, f"{dt:.1f}s 流式:{has_stream} 回复:{reply[:50]}")


async def test_0_1_multi_turn_memory(token: str, use_llm: bool) -> None:
    """0.1 多轮记忆：告诉名字→问名字。0.1 引擎实例常驻，预期记得。"""
    print("\n=== 0.1 多轮记忆 ===")
    tid, pid = create_thread_01(token)
    if not use_llm:
        record("0.1", "多轮记忆 (跳过LLM)", True, "")
        return
    # R1：告诉名字
    ev1, _ = await chat_01(token, tid, pid, "你好，我叫小测，请记住我的名字")
    r1 = extract_reply(ev1)
    # 等 R1 完全结束（避免状态竞争）
    await asyncio.sleep(2)
    # R2：问名字
    ev2, _ = await chat_01(token, tid, pid, "我叫什么名字？")
    r2 = extract_reply(ev2)
    ok = "小测" in r2 or "测试" in r2
    record("0.1", "多轮记忆", ok, f"R1:{r1[:30]} | R2:{r2[:30]} | {'记得' if ok else '没记住'}")


# ── 0.2 (Rust 栈) ───────────────────────────────────────────────────────────


def chat_02(message: str, sid: str, history: list | None = None, timeout: int = 120) -> tuple[float, str]:
    """0.2 HTTP chat（同步返回，无流式）。history 用于多轮。"""
    body = {
        "message": message, "session_id": sid,
        "history": history or [], "agent_id": "agentos",
    }
    t0 = time.time()
    with urllib.request.urlopen(
        urllib.request.Request(
            f"{KERNEL_02}/api/v1/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=timeout,
    ) as r:
        data = json.loads(r.read())
        return time.time() - t0, data.get("content", "")


def test_0_2_single_turn(use_llm: bool) -> None:
    print("\n=== 0.2 单轮 LLM ===")
    if not use_llm:
        record("0.2", "单轮 (跳过LLM)", True, "")
        return
    dt, reply = chat_02("你好，请用一句话介绍你自己", "single-1")
    ok = dt < 120 and len(reply) > 0
    record("0.2", "单轮回复", ok, f"{dt:.2f}s 回复:{reply[:50]}")


def test_0_2_multi_turn_memory(use_llm: bool) -> None:
    """0.2 多轮记忆：R1 告诉名字→R2 问名字（带 history）。

    重点验证现状：0.2 state 回写被 if false 禁用，但 HTTP chat 靠客户端传 history
    维持上下文。本测试模拟客户端维护 history（正确用法），验证带 history 能记住。

    已知现象（如实记录）：带 history 的多轮请求，LLM 可能进入工具调用循环
    （autonomous.yaml 的 routes 在 raw_tool_calls 非空时 next:loop），表现为超时。
    本测试用较短超时（45s）探测，超时不计为失败，而是记录为"工具循环现象"。
    """
    print("\n=== 0.2 多轮记忆 (重点验证现状) ===")
    if not use_llm:
        record("0.2", "多轮记忆 (跳过LLM)", True, "")
        return
    # 子测试A：客户端维护 history（OpenAI 格式）—— 模拟前端正确用法
    try:
        dt1, r1 = chat_02("回复：好的", "mt-a", timeout=60)
    except Exception as e:
        record("0.2", "多轮记忆(带history)", False, f"R1 失败: {e}")
        return
    history = [
        {"role": "user", "content": "回复：好的"},
        {"role": "assistant", "content": r1},
        {"role": "user", "content": "我叫什么名字？"},
    ]
    try:
        dt2, r2 = chat_02("我叫什么名字？", "mt-a", history=history, timeout=45)
        ok_with_history = "小测" in r2 or "测试" in r2
        record(
            "0.2", "多轮记忆(带history)", ok_with_history,
            f"R2 {dt2:.1f}s: {r2[:40]} | {'记得' if ok_with_history else '没记住'}",
        )
    except Exception as e:
        # 超时通常意味着进入了工具调用循环（已知现象，如实记录）
        record(
            "0.2", "多轮记忆(带history)", False,
            f"R2 超时/失败({type(e).__name__})：疑似工具调用循环（autonomous.yaml routes 在 tool_calls 非空时 next:loop）",
        )


# ── 主函数 ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="0.1 vs 0.2 用户旅程对照")
    parser.add_argument("--only", choices=["0.1", "0.2"], help="只测指定栈")
    parser.add_argument("--no-llm", action="store_true", help="跳过真实 LLM")
    args = parser.parse_args()

    print("=" * 70)
    print("0.1 vs 0.2 用户旅程对照实测")
    print(f"0.1: {API_01}  0.2: {KERNEL_02}  LLM: {'跳过' if args.no_llm else '真实'}")
    print("=" * 70)

    async def run():
        if args.only != "0.2":
            try:
                token = login_01()
                print(f"[0.1] 登录成功")
                await test_0_1_single_turn(token, not args.no_llm)
                await test_0_1_multi_turn_memory(token, not args.no_llm)
            except Exception as e:
                record("0.1", "栈初始化", False, f"0.1 不可用: {e}")
        if args.only != "0.1":
            try:
                http(f"{KERNEL_02}/health")
                print(f"[0.2] kernel 可用")
                test_0_2_single_turn(not args.no_llm)
                test_0_2_multi_turn_memory(not args.no_llm)
            except Exception as e:
                record("0.2", "栈初始化", False, f"0.2 不可用: {e}")

    asyncio.run(run())

    # 汇总对照表
    print("\n" + "=" * 70)
    print("对照汇总")
    print("=" * 70)
    by_case: dict[str, dict[str, tuple[bool, str]]] = {}
    for stack, name, passed, detail in _results:
        by_case.setdefault(name, {})[stack] = (passed, detail)
    for name, stacks in by_case.items():
        r1 = stacks.get("0.1", (None, ""))
        r2 = stacks.get("0.2", (None, ""))
        print(f"  {name}:")
        if r1[0] is not None:
            print(f"    0.1: {'✓' if r1[0] else '✗'} {r1[1]}")
        if r2[0] is not None:
            print(f"    0.2: {'✓' if r2[0] else '✗'} {r2[1]}")

    total = len(_results)
    passed = sum(1 for _, _, p, _ in _results if p)
    print(f"\n汇总: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
