"""Web 端到端测试 V2 — 使用跨平台命令验证 bash_check 评估。

修复 V1 的问题：
- Windows 上 `date` 命令是交互式的（等待输入），导致超时
- 改用 `python -c "import datetime; print(datetime.datetime.now())"`
- 同时验证 bash_check 评估流程是否正常工作
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import os
os.environ["PYTHONIOENCODING"] = "utf-8"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            "logs/test_web_e2e_v2.log", mode="w", encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("web_e2e_v2")

BASE_URL = "http://127.0.0.1:8888"
WS_URL = "ws://127.0.0.1:8888"

# 跨平台时间获取命令
TIME_CMD = 'python -c "import datetime; print(datetime.datetime.now())"'


async def register_and_login() -> str:
    """注册用户并获取 JWT token。"""
    username = f"e2e_v2_{int(time.time())}"
    password = "test123456"

    # Register
    data = json.dumps({
        "username": username,
        "password": password,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/auth/register",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read().decode())
        token = result.get("token", "")
        if token:
            logger.info("Registered + auto-logged in as %s", username)
            return token
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        logger.info("Register response: %s %s", e.code, body)

    # Login
    data = json.dumps({
        "username": username,
        "password": password,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/auth/login",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    result = json.loads(resp.read().decode())
    token = result.get("access_token", result.get("token", ""))
    logger.info("Logged in as %s, token=%s...", username, token[:20])
    return token


async def create_thread(token: str) -> str:
    """创建对话线程。"""
    data = json.dumps({"title": "E2E V2 Test"}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/threads",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    result = json.loads(resp.read().decode())
    thread_id = result.get("thread_id", result.get("id", ""))
    logger.info("Created thread: %s", thread_id)
    return thread_id


async def websocket_test(token: str, thread_id: str) -> dict:
    """通过 WebSocket 发送消息并收集响应事件。"""
    try:
        import websockets
    except ImportError:
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "websockets"],
            timeout=60,
        )
        import websockets

    uri = f"{WS_URL}/ws/chat/{thread_id}?token={token}"
    events = []

    async with websockets.connect(uri) as ws:
        logger.info("WebSocket connected to %s", uri)

        # 收集初始事件
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        evt = json.loads(msg)
        events.append(evt)
        logger.info("Initial event: %s", evt.get("type", "unknown"))

        # 发送测试消息 — 使用跨平台命令
        test_msg = (
            f"请用 task_submit 提交一个短期任务给 general_agent，"
            f"目标是获取当前时间，验收标准用 bash_check，"
            f"命令用 `{TIME_CMD}`"
        )
        await ws.send(json.dumps({
            "type": "user_input",
            "content": test_msg,
        }))
        logger.info("Sent: %s", test_msg[:80])

        # 收集事件直到 stream_end 或 new_message
        deadline = time.time() + 300  # 5 min max wait
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(
                    ws.recv(), timeout=min(30, deadline - time.time())
                )
                evt = json.loads(msg)
                events.append(evt)
                evt_type = evt.get("type", "unknown")
                logger.info("Event: %s", evt_type)

                if evt_type in ("stream_end", "new_message"):
                    # Wait a bit more for potential follow-up events
                    try:
                        extra = await asyncio.wait_for(
                            ws.recv(), timeout=5
                        )
                        events.append(json.loads(extra))
                    except asyncio.TimeoutError:
                        pass
                    break

            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for event")
                break

    return {"events": events, "event_count": len(events)}


async def check_server_logs() -> dict:
    """分析服务端日志，提取关键检查点。"""
    log_dir = PROJECT_ROOT / "logs"
    checkpoints = {}

    # 查找最新的包含 task_evaluate 的管道日志
    pipeline_logs = sorted(
        log_dir.glob("pipeline_*.log"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    for log_path in pipeline_logs[:3]:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        if "task_evaluate" not in content:
            continue

        task_id = None
        for line in content.splitlines():
            if "Task created:" in line:
                parts = line.split("Task created:")
                if len(parts) > 1:
                    task_id = parts[1].strip().split()[0]
            if "task.submitted" in line and "subscribers" in line:
                checkpoints["CP2_eventbus"] = True
            if "Expect evaluation:" in line:
                checkpoints["CP5_expect_result"] = line.strip()
            if "Tool evaluation completed:" in line:
                checkpoints["CP4_tool_eval"] = line.strip()
            if "task_evaluate result:" in line:
                checkpoints["CP6_tevaluate_result"] = line.strip()
            if "Failed conditions" in line:
                checkpoints["CP5_failed_conditions"] = line.strip()
            if "evaluation: failed" in line or "evaluation: passed" in line:
                checkpoints["CP7_final_eval"] = line.strip()
            if "Route applied: end" in line:
                checkpoints["CP8_route_end"] = line.strip()

        if task_id:
            checkpoints["task_id"] = task_id
        break  # 只检查最新一个

    return checkpoints


async def check_task_data(task_id: str | None) -> dict:
    """检查任务数据。"""
    if not task_id:
        return {"error": "No task_id found"}

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from tasks.service import TaskService

    ts = TaskService()
    task = ts.get_task(task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}

    status = task.status.value if hasattr(task.status, "value") else str(task.status)
    result = {
        "id": task.id,
        "status": status,
        "title": task.title,
    }

    if task.metadata:
        ac = task.metadata.get("acceptance_criteria", {})
        result["acceptance_criteria"] = str(ac)[:200]
        metrics = task.metadata.get("evaluation_metric_ids", [])
        result["metrics"] = metrics

    if task.result:
        result["result_summary"] = str(task.result)[:300]

    return result


async def main():
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)

    print("\n" + "=" * 60)
    print("Web E2E Test V2 — bash_check 跨平台验证")
    print("=" * 60)

    results = []

    # Step 1: Register + Login
    print("\n--- Step 1: 注册 + 登录 ---")
    try:
        token = await register_and_login()
        results.append(("注册+登录", True, f"token={token[:20]}..."))
    except Exception as e:
        results.append(("注册+登录", False, str(e)))
        print(f"  [FAIL] {e}")
        _print_summary(results)
        return False

    # Step 2: Create thread
    print("\n--- Step 2: 创建对话线程 ---")
    try:
        thread_id = await create_thread(token)
        results.append(("创建线程", True, f"thread={thread_id[:20]}..."))
    except Exception as e:
        results.append(("创建线程", False, str(e)))
        _print_summary(results)
        return False

    # Step 3: WebSocket test
    print("\n--- Step 3: WebSocket 消息交互 ---")
    try:
        ws_result = await websocket_test(token, thread_id)
        evt_count = ws_result["event_count"]
        evt_types = [e.get("type", "?") for e in ws_result["events"]]
        results.append((
            "WebSocket交互",
            evt_count >= 3,
            f"events={evt_count}, types={evt_types}",
        ))
        for e in ws_result["events"]:
            if e.get("type") == "stream_chunk":
                content = e.get("content", "")
                if content:
                    print(f"  AI回复: {content[:200]}")
    except Exception as e:
        results.append(("WebSocket交互", False, str(e)))
        logger.exception("WebSocket test failed")

    # Step 4: Wait for task completion + check logs
    print("\n--- Step 4: 等待任务完成 + 检查日志 ---")
    print("  等待 60 秒让任务执行和评估完成...")
    await asyncio.sleep(60)

    checkpoints = await check_server_logs()
    print(f"  检查点: {len(checkpoints)} 项")
    for k, v in checkpoints.items():
        if isinstance(v, str) and len(v) > 100:
            print(f"    {k}: {v[:100]}...")
        else:
            print(f"    {k}: {v}")

    task_id = checkpoints.get("task_id")
    if task_id:
        results.append(("任务ID获取", True, task_id))
    else:
        results.append(("任务ID获取", False, "未找到"))

    # Step 5: Check task data
    print("\n--- Step 5: 任务数据检查 ---")
    task_data = await check_task_data(task_id)
    print(f"  任务状态: {task_data.get('status', 'N/A')}")
    print(f"  任务标题: {task_data.get('title', 'N/A')}")
    if "metrics" in task_data:
        print(f"  评估指标: {task_data['metrics']}")
    if "result_summary" in task_data:
        print(f"  结果摘要: {task_data['result_summary'][:200]}")

    status = task_data.get("status", "")
    passed = status in ("completed",)
    results.append((
        "任务完成",
        passed,
        f"status={status}",
    ))

    # Step 6: Check evaluation result
    print("\n--- Step 6: 评估结果验证 ---")
    expect_line = checkpoints.get("CP5_expect_result", "")
    if "passed=True" in expect_line:
        results.append(("bash_check评估", True, "passed=True"))
        print("  [PASS] bash_check 评估通过")
    elif "passed=False" in expect_line:
        failed = checkpoints.get("CP5_failed_conditions", "")
        results.append(("bash_check评估", False, f"passed=False: {failed[:100]}"))
        print(f"  [FAIL] bash_check 评估未通过: {failed[:100]}")
    else:
        results.append(("bash_check评估", False, f"未找到评估日志"))
        print("  [WARN] 未找到 bash_check 评估日志")

    _print_summary(results)
    passed_count = sum(1 for _, ok, _ in results if ok)
    return passed_count == len(results)


def _print_summary(results):
    print("\n" + "=" * 60)
    print("验证结果汇总")
    print("=" * 60)
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n  总计: {passed}/{total} 通过")
    if passed == total:
        print("\n  所有验证项通过！")
    else:
        print(f"\n  {total - passed} 项失败")


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
