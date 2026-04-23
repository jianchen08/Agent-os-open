"""Web E2E 测试 — 通过灵犀(L1)自然对话触发工具创建全流程。"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
import os
os.environ["PYTHONIOENCODING"] = "utf-8"

BASE_URL = "http://127.0.0.1:8888"


async def get_token() -> str:
    username = f"sci_test_{int(time.time())}"
    data = json.dumps({"username": username, "password": "test123"}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/auth/register",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return json.loads(resp.read())["access_token"]
    except Exception:
        req = urllib.request.Request(
            f"{BASE_URL}/api/v1/auth/login",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        return json.loads(urllib.request.urlopen(req, timeout=5).read())["access_token"]


async def create_thread(token: str) -> str:
    data = json.dumps({"title": "Sci Calc Test"}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/threads",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=5).read())["thread_id"]


async def send_message(token: str, thread_id: str):
    """发送自然语言消息给灵犀，不指定任何 agent 或 task_submit。"""
    import websockets
    uri = f"ws://127.0.0.1:8888/ws/chat/{thread_id}?token={token}"
    async with websockets.connect(uri) as ws:
        await ws.recv()  # connection_confirmation
        await ws.send(json.dumps({
            "type": "user_input",
            "content": "请帮我创建一个科学计算器工具，支持三角函数、对数、幂运算等功能。不需要确认，直接派发执行。",
        }))
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                evt = json.loads(msg)
                t = evt.get("type", "")
                print(f"  WS event: {t}", flush=True)
                if t in ("stream_end", "new_message"):
                    break
            except asyncio.TimeoutError:
                break


async def monitor_logs(wait_seconds: int = 600):
    """Monitor logs for task completion with all metrics."""
    logs_dir = PROJECT_ROOT / "logs"
    print(f"\n  等待最多 {wait_seconds} 秒让任务执行完成...", flush=True)

    start = time.time()
    found_metrics = set()
    task_id = None

    while time.time() - start < wait_seconds:
        await asyncio.sleep(20)
        elapsed = int(time.time() - start)

        pipeline_logs = sorted(
            logs_dir.glob("pipeline_*.log"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

        for log_path in pipeline_logs[:3]:
            if time.time() - log_path.stat().st_mtime > elapsed + 10:
                continue
            content = log_path.read_text(encoding="utf-8", errors="replace")
            if "task_evaluate" not in content and "TaskEvaluate" not in content:
                continue

            for line in content.splitlines():
                # Track task ID
                if not task_id:
                    m = re.search(r"task_id=([a-f0-9]+)", line)
                    if m and "TaskEvaluate" in line:
                        task_id = m.group(1)

                # Track each metric evaluation
                if "Expect evaluation:" in line and "evaluation.engine" in line:
                    m = re.search(r"Expect evaluation: (\w+) -> passed=(\w+)", line)
                    if m:
                        metric, passed = m.group(1), m.group(2)
                        if metric not in found_metrics:
                            found_metrics.add(metric)
                            print(f"  [{elapsed}s] 指标评估: {metric} -> passed={passed}", flush=True)

                # Track tool evaluation
                if "Tool evaluation completed:" in line:
                    m = re.search(r"Tool evaluation completed: (\w+)", line)
                    if m:
                        metric = m.group(1)
                        if metric not in found_metrics:
                            print(f"  [{elapsed}s] 工具评估完成: {metric}", flush=True)

                # Track agent evaluation
                if "Agent evaluation:" in line and "launching" in line:
                    m = re.search(r"Agent evaluation: (\w+)", line)
                    if m:
                        metric = m.group(1)
                        print(f"  [{elapsed}s] Agent评估启动: {metric}", flush=True)

                # Track task status
                if "evaluation: passed" in line and "tasks.service" in line:
                    print(f"  [{elapsed}s] 任务评估通过!", flush=True)
                    return True
                if "evaluation: failed" in line and "tasks.service" in line:
                    print(f"  [{elapsed}s] 任务评估失败", flush=True)
                    return False

            break  # only check newest log

    print(f"\n  已收集指标: {found_metrics}", flush=True)
    if not found_metrics:
        print(f"  超时 ({wait_seconds}s)，未找到评估日志", flush=True)
    return False


async def main():
    print("=" * 60)
    print("工具创建全流程测试（通过灵犀自然对话）")
    print("验证: file_check + test_check + function_verify")
    print("=" * 60)

    print("\n--- Step 1: 获取 token + 创建线程 ---")
    token = await get_token()
    thread_id = await create_thread(token)
    print(f"  token=...{token[-20:]}, thread={thread_id[:20]}...")

    print("\n--- Step 2: 发送自然语言消息给灵犀 ---")
    await send_message(token, thread_id)

    print("\n--- Step 3: 监控日志等待完成 ---")
    passed = await monitor_logs(wait_seconds=600)

    print("\n" + "=" * 60)
    if passed:
        print("结果: 工具创建 + 测试验证 + 功能评估 全部通过!")
    else:
        print("结果: 任务未通过，需要继续修复")
    print("=" * 60)
    return passed


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
