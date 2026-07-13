#!/usr/bin/env python
"""测试 Agent 创建小说大纲生成 Agent 的完整流程（含后台任务闭环）。"""
import asyncio
import logging
import time
from channels.cli.cli_main import CLIApplication

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


async def run_e2e_test():
    """端到端测试：创建小说大纲生成 Agent 的完整流程（含后台任务闭环）。

    注意：此函数需要 300s 超时，应通过 python test_agent_create.py 手动运行。
    """
    t0 = time.time()

    # 1. 初始化
    print("=" * 60)
    print("PHASE 1: INITIALIZATION")
    print("=" * 60)

    app = CLIApplication()
    app.setup_pipeline()

    tw = getattr(app, "_task_worker", None)
    if not tw:
        print("FATAL: TaskWorker not initialized!")
        return
    await tw.start()
    print(f"Pipeline initialized. Agent: {app._agent_config.config_id}\n")

    # 2. 发送任务给主 Agent (L1)
    print("=" * 60)
    print("PHASE 2: SUBMIT TASK TO L1")
    print("=" * 60)

    user_input = "创建一个生成网络小说大纲的 agent。"
    print(f"User: {user_input}\n")

    try:
        result = await asyncio.wait_for(
            app._engine.run(
                user_input=user_input,
                agent_config=app._agent_config,
                conversation_history=None,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            ),
            timeout=300,
        )
    except asyncio.TimeoutError:
        print("TIMEOUT: L1 execution exceeded 300s")
        await tw.stop()
        return

    elapsed_l1 = time.time() - t0
    iters_l1 = result.get("iteration", 0)
    task_id = result.get("submitted_task_id", "")
    pipeline_id = result.get("pipeline_id", "")
    raw = result.get("raw_result", "")

    print(f"\nL1 done: {elapsed_l1:.1f}s, {iters_l1} iterations")
    print(f"Pipeline ID: {pipeline_id}")
    print(f"Submitted Task ID: {task_id}")
    print(f"LLM response (first 300 chars): {(str(raw))[:300]}")

    # 3. 等待后台任务完成 (L2 -> L3)
    print("\n" + "=" * 60)
    print("PHASE 3: WAIT FOR BACKGROUND TASKS (L2->L3)")
    print("=" * 60)

    wait_time = 300
    print(f"Waiting up to {wait_time}s for background tasks...")

    final_status = "unknown"
    for i in range(wait_time // 10):
        await asyncio.sleep(10)
        elapsed = time.time() - t0
        if (i + 1) % 3 == 0:
            print(f"  [{elapsed:.0f}s] checking task status...", flush=True)

        if task_id and tw:
            try:
                ts = app._services.get("task_service")
                if ts:
                    task = ts.get_task(task_id)
                    if task:
                        status = task.status if hasattr(task, "status") else task.get("status", "?")
                        print(f"  [{elapsed:.0f}s] Task {task_id} status: {status}")
                        if status in ("completed", "failed", "cancelled"):
                            final_status = status
                            break
            except Exception as e:
                print(f"  [{elapsed:.0f}s] Error checking task: {e}")

    # 4. 结果
    print("\n" + "=" * 60)
    print("PHASE 4: RESULT")
    print("=" * 60)
    print(f"Total time: {time.time() - t0:.1f}s")
    print(f"L1 iterations: {iters_l1}")
    print(f"Pipeline ID: {pipeline_id}")
    print(f"Task ID: {task_id}")
    print(f"Task final status: {final_status}")

    # 5. 清理
    await tw.stop()
    print("\nTest complete.")


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
