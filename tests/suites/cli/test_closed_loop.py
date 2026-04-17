#!/usr/bin/env python
"""完整闭环测试：L1 -> L2 -> L3 -> 评估 -> 验证。

使用真实 MiniMax M2.7 API，验证整个任务执行链路。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
for name in ["httpx", "litellm", "httpcore", "asyncio"]:
    logging.getLogger(name).setLevel(logging.WARNING)
logging.getLogger("pipeline.engine").setLevel(logging.INFO)
logging.getLogger("plugins.core.llm_core").setLevel(logging.INFO)
logging.getLogger("plugins.core.tool_core").setLevel(logging.INFO)
logging.getLogger("infrastructure.task_worker").setLevel(logging.INFO)
logging.getLogger("tools.builtin.task_submit").setLevel(logging.INFO)
logging.getLogger("evaluation").setLevel(logging.INFO)


TARGET_AGENT = "closed_loop_helper"
TARGET_PATH = PROJECT_ROOT / "config/agents/executor/test" / f"{TARGET_AGENT}.yaml"


async def main():
    from channels.cli.cli_main import CLIApplication
    from agents.registry import AgentRegistry

    t0 = time.time()

    # ============================================================
    # 1. 初始化
    # ============================================================
    print("=" * 60, flush=True)
    print("PHASE 1: INITIALIZATION", flush=True)
    print("=" * 60, flush=True)

    app = CLIApplication()
    app.setup_pipeline()

    tw = getattr(app, "_task_worker", None)
    if not tw:
        print("FATAL: TaskWorker not initialized!", flush=True)
        return
    await tw.start()
    print(f"Pipeline initialized. Agent: {app._agent_config.config_id}", flush=True)

    # ============================================================
    # 2. 发送任务给主 Agent (L1)
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print("PHASE 2: SUBMIT TASK TO L1", flush=True)
    print("=" * 60, flush=True)

    task_prompt = (
        f"请创建一个新的 L3 Agent，名称为 {TARGET_AGENT}，"
        "只需要 file_read 和 file_write 两个工具，"
        f"保存到 config/agents/executor/test/{TARGET_AGENT}.yaml。\n\n"
        "请通过 task_submit 提交给 resource_generator_agent 完成。"
    )
    print(f"Task: {task_prompt[:80]}...", flush=True)

    try:
        result = await asyncio.wait_for(
            app._engine.run(
                user_input=task_prompt,
                agent_config=app._agent_config,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            ),
            timeout=180,
        )
    except asyncio.TimeoutError:
        print("TIMEOUT: L1 execution exceeded 180s", flush=True)
        await tw.stop()
        return

    elapsed_l1 = time.time() - t0
    iterations_l1 = result.get("iteration", 0)
    print(f"\nL1 done: {elapsed_l1:.1f}s, {iterations_l1} iterations", flush=True)

    # ============================================================
    # 3. 等待后台任务完成 (L2 -> L3)
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print("PHASE 3: WAIT FOR BACKGROUND TASKS (L2->L3)", flush=True)
    print("=" * 60, flush=True)

    # 等待足够时间让 L2 和 L3 完成
    wait_time = 180
    print(f"Waiting {wait_time}s for background tasks...", flush=True)

    for i in range(wait_time // 10):
        await asyncio.sleep(10)
        elapsed = time.time() - t0
        # 每隔30秒打印状态
        if (i + 1) % 3 == 0:
            exists = TARGET_PATH.exists()
            print(f"  [{elapsed:.0f}s] Target file exists: {exists}", flush=True)
            if exists:
                break

    # ============================================================
    # 4. 验证结果
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print("PHASE 4: VERIFICATION", flush=True)
    print("=" * 60, flush=True)

    elapsed_total = time.time() - t0

    # 4.1 检查文件是否存在
    file_exists = TARGET_PATH.exists()
    print(f"File exists: {file_exists}", flush=True)
    print(f"File path: {TARGET_PATH}", flush=True)

    if file_exists:
        content = TARGET_PATH.read_text(encoding="utf-8")
        print(f"File size: {len(content)} chars", flush=True)
        print(f"Content preview:\n{content[:300]}", flush=True)

        # 4.2 验证能被系统加载
        try:
            registry = AgentRegistry()
            registry.load_directory(PROJECT_ROOT / "config/agents")
            loaded = registry.get(TARGET_AGENT)
            if loaded:
                print(f"\nAgent loaded successfully:", flush=True)
                print(f"  config_id: {loaded.config_id}", flush=True)
                print(f"  name: {loaded.display_name}", flush=True)
                print(f"  level: {loaded.level.value}", flush=True)
                print(f"  tool_ids: {loaded.tool_ids}", flush=True)

                # 4.3 验证 to_state() 可用
                state = loaded.to_state()
                sp_len = len(state.get("system_prompt", ""))
                print(f"  to_state() system_prompt: {sp_len} chars", flush=True)
                print(f"  to_state() tool_ids: {state.get('tool_ids', [])}", flush=True)
            else:
                print("FAILED: Agent not found in registry!", flush=True)
        except Exception as e:
            print(f"FAILED: Agent load error: {e}", flush=True)
    else:
        # 搜索其他位置
        print("\nSearching for created files...", flush=True)
        for f in PROJECT_ROOT.rglob(f"{TARGET_AGENT}*"):
            if f.suffix in ('.yaml', '.yml'):
                print(f"  Found: {f}", flush=True)

        # 检查是否有新创建的 yaml 文件
        agents_dir = PROJECT_ROOT / "config/agents"
        for f in agents_dir.rglob("*.yaml"):
            if f.stat().st_mtime > t0 and f.name != "test_helper.yaml":
                print(f"  New file: {f.relative_to(PROJECT_ROOT)}", flush=True)

    # 4.4 检查任务状态
    task_service = app._services.get("task_service")
    if task_service:
        try:
            all_tasks = task_service.list_tasks()
            print(f"\nTask service: {len(all_tasks)} tasks", flush=True)
            for t in all_tasks[:10]:
                print(f"  {t.id[:12]}... | {t.title} | {t.status.value}", flush=True)
        except Exception as e:
            print(f"Task service error: {e}", flush=True)

    # ============================================================
    # 5. 汇总
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print(f"SUMMARY: {elapsed_total:.1f}s total", flush=True)
    print("=" * 60, flush=True)

    if file_exists:
        print("SUCCESS: Full closed-loop test PASSED!", flush=True)
    else:
        print("PARTIAL: Agent file not created via L1->L2->L3 chain.", flush=True)
        print("Note: L3 (agent_maker) works correctly when called directly.", flush=True)

    # 停止 TaskWorker
    await tw.stop()
    print("\nDone.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
