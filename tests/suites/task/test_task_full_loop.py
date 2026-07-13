"""端到端测试：完整任务闭环。

测试流程：
1. 初始化 CLI（真实 LLM 模式）
2. 灵汐 Agent 调用 task_submit 提交任务
3. TaskWorker 接收事件并执行子 Agent
4. 子 Agent 完成后调用 task_evaluate
5. TaskService 发布终态事件
6. TaskEventReceiverPlugin 接收事件并注入通知

验证点：
- 任务创建成功（pending）
- Worker 启动任务（running）
- 子 Agent 执行（LLM 真实调用）
- 评估完成（completed/failed）
- 事件通知机制工作
"""

import asyncio
import logging
import sys
import pytest

# 设置 PYTHONPATH
sys.path.insert(0, "src")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@pytest.mark.integration
@pytest.mark.skip(reason="依赖已移除的 task_submit_func API，需要 LLM API key")
async def test_full_task_loop():
    """测试完整任务闭环。"""
    print("=" * 60)
    print("完整任务闭环测试（真实 LLM）")
    print("=" * 60)

    # 1. 初始化 CLI
    from channels.cli.cli_main import CLIApplication

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    # 2. 手动启动 Worker（不在 run() 循环中）
    if hasattr(app, '_task_worker') and app._task_worker:
        await app._task_worker.start()
        print("[OK] TaskWorker 已启动")
    else:
        print("[FAIL] TaskWorker 未初始化")
        return

    # 3. 检查共享服务
    task_service = app._services.get("task_service")
    event_bus = app._services.get("event_bus")
    print(f"[INFO] 共享 TaskService: {type(task_service).__name__}")
    print(f"[INFO] 共享 EventBus: {type(event_bus).__name__}")

    # 4. 提交一个简单的任务
    print("\n--- 提交任务 ---")
    from tools.builtin.task_submit import task_submit_func

    submit_params = {
        "target_type": "agent",
        "target_id": "lingxi",
        "goal": {
            "title": "测试任务：说hello",
            "description": "请回复 hello world",
        },
        "acceptance_criteria": {
            "basic_check": {"pass_threshold": 50},
        },
        "_task_service": task_service,
    }

    result = task_submit_func(submit_params)
    print(f"提交结果: success={result.get('success')}")
    print(f"任务ID: {result.get('task_id')}")
    print(f"任务状态: {result.get('status')}")

    if not result.get("success"):
        print(f"[FAIL] 任务提交失败: {result.get('error')}")
        return

    task_id = result["task_id"]
    print(f"\n[OK] 任务已提交: {task_id}")

    # 5. 等待 Worker 执行任务
    print("\n--- 等待任务执行 ---")
    print("等待 Worker 拾取任务并执行...")

    # 等待最多 60 秒
    for i in range(30):
        await asyncio.sleep(2)
        task = task_service.get_task(task_id)
        if task:
            status = task.status.value
            print(f"  [{i*2}s] 任务状态: {status}")
            if status in ("completed", "failed"):
                break
        else:
            print(f"  [{i*2}s] 任务不存在!")
            break

    # 6. 检查最终状态
    task = task_service.get_task(task_id)
    if task:
        print(f"\n--- 任务最终状态 ---")
        print(f"  ID: {task.id}")
        print(f"  标题: {task.title}")
        print(f"  状态: {task.status.value}")
        print(f"  结果: {str(task.result)[:200] if task.result else '无'}")
        print(f"  错误: {task.error or '无'}")

        if task.status.value == "completed":
            print("\n[PASS] 任务完成!")
        elif task.status.value == "failed":
            print(f"\n[WARN] 任务失败: {task.error}")
        else:
            print(f"\n[WARN] 任务未到终态: {task.status.value}")
    else:
        print("\n[FAIL] 任务不存在!")

    # 7. 检查事件机制
    print("\n--- 检查事件机制 ---")
    # 手动触发一个状态变更，检查事件是否发布
    event_received = []

    async def on_test_event(data):
        event_received.append(data)

    event_bus.subscribe("task_state_changed", on_test_event)

    # 创建另一个任务来触发事件
    task_service.create_task(title="事件测试任务")
    await asyncio.sleep(1)

    if event_received:
        print(f"[OK] 收到事件: {len(event_received)} 个")
        for ev in event_received:
            print(f"  - task_id={ev.get('task_id')}, new_status={ev.get('new_status')}")
    else:
        print("[WARN] 未收到事件（可能回调未正确设置）")

    event_bus.unsubscribe("task_state_changed", on_test_event)

    # 8. 清理
    if hasattr(app, '_task_worker') and app._task_worker:
        await app._task_worker.stop()

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_full_task_loop())
