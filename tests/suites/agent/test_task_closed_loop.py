#!/usr/bin/env python
"""Agent OS 任务执行闭环测试 — 验证端到端任务产出。

完整闭环链路：
  用户发消息 → LLM 调用 task_submit → TaskWorker 拾取 → general_agent 执行 → 产出文件 → completed

不使用 Mock，直接复用 CLIApplication，与真实启动完全一致。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

OUTPUT_DIR = Path("data/tasks/e2e_output")


async def main() -> None:
    print("=" * 60)
    print("  任务执行闭环测试")
    print("  用户发消息 → task_submit → TaskWorker → 产出 → completed")
    print("=" * 60)

    # 确保工作目录为项目根
    os.chdir(_PROJECT_ROOT)

    start_time = time.time()

    # ── 1. 初始化 CLIApplication ──
    print("\n[1/5] 初始化 CLIApplication...")
    from channels.cli.cli_main import CLIApplication
    app = CLIApplication()
    app.setup_pipeline()
    print(f"  服务数: {len(app._services)}")

    # ── 2. 启动 TaskWorker ──
    print("\n[2/5] 启动 TaskWorker...")
    tw = getattr(app, "_task_worker", None)
    if tw and hasattr(tw, "start"):
        await tw.start()
        print("  TaskWorker 启动成功")
    else:
        print("  ❌ 无 TaskWorker")
        return

    task_service = app._services.get("task_service")
    if not task_service:
        print("  ❌ 无 task_service")
        return

    # ── 3. 发消息让 Agent 提交任务 ──
    output_path = OUTPUT_DIR / "hello.txt"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    task_description = (
        f"请在文件 {output_path} 中写入一行文字：'Hello from E2E closed loop test'，"
        f"然后使用 task_evaluate 的 auto_complete 模式完成任务。"
    )

    print(f"\n[3/5] 发消息给 Agent：创建一个写文件的任务...")
    print(f"  任务内容: 写入文件 {output_path}")

    result = await app._engine.run(
        user_input=(
            "请帮我创建一个短期任务，交给 general_agent 执行。\n"
            f"任务标题：E2E闭环测试-写文件\n"
            f"任务描述：{task_description}\n"
            f"验收标准：文件 {output_path} 必须存在，且包含 'Hello from E2E'。"
        ),
        agent_config=app._agent_config,
        conversation_history=None,
        streaming=False,
        auto_approve=True,
        interaction_mode="auto",
    )

    # ── 4. 提取 task_id ──
    task_id = None
    raw_result = str(result.get("raw_result", ""))
    for tr in result.get("tool_results", []):
        if tr.get("tool_name") == "task_submit" and tr.get("success"):
            data = tr.get("data", {})
            task_id = data.get("task_id") or data.get("output", {}).get("task_id", "")
            print(f"  task_submit 返回: {json.dumps(data, ensure_ascii=False, default=str)[:200]}")
            break

    if not task_id:
        # 尝试从 task_service 中找最新任务
        all_tasks = task_service.list_all()
        recent = [t for t in all_tasks if "E2E闭环" in getattr(t, "title", "")]
        if recent:
            recent.sort(key=lambda t: getattr(t, "created_at", ""), reverse=True)
            task_id = recent[0].id
            print(f"  从 TaskService 找到最新任务: {task_id}")
        else:
            print(f"  ❌ 未找到任务 ID")
            print(f"  raw_result: {raw_result[:300]}")
            await tw.stop()
            return

    print(f"  任务 ID: {task_id}")

    # ── 5. 等待任务完成 + 轮询检查 ──
    print(f"\n[4/5] 等待 TaskWorker 执行任务...")
    from tasks.types import TaskStatus

    max_wait = 300
    poll_interval = 5
    elapsed_wait = 0
    final_status = "unknown"

    while elapsed_wait < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed_wait += poll_interval

        task = task_service.get_task(task_id)
        if task is None:
            print(f"  ⚠️ 任务 {task_id} 不存在")
            break

        status_val = task.status.value if hasattr(task.status, "value") else str(task.status)

        if status_val in ("completed", "failed", "cancelled"):
            final_status = status_val
            print(f"  ✅ 任务终态: {status_val} (等待 {elapsed_wait}s)")
            break
        else:
            if elapsed_wait % 15 == 0:
                print(f"  ... 任务状态: {status_val} ({elapsed_wait}s)")

    if final_status == "unknown":
        task = task_service.get_task(task_id)
        if task:
            final_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        print(f"  ⏰ 超时 ({max_wait}s), 最终状态: {final_status}")

    # ── 6. 检查产出 ──
    print(f"\n[5/5] 检查任务产出...")

    # 读取完整任务信息
    task = task_service.get_task(task_id)
    print(f"  标题: {task.title if task else '?'}")
    print(f"  状态: {final_status}")
    print(f"  结果: {str(getattr(task, 'result', None))[:200] if task else '?'}")
    print(f"  错误: {str(getattr(task, 'error', None))[:200] if task else '?'}")

    # 检查产出文件
    if output_path.exists():
        content = output_path.read_text(encoding="utf-8")
        has_hello = "Hello from E2E" in content
        print(f"  产出文件: {output_path} ✅ 存在")
        print(f"  文件内容 ({len(content)} 字符): {content[:100]}")
        if has_hello:
            print(f"  内容验证: ✅ 包含 'Hello from E2E'")
        else:
            print(f"  内容验证: ⚠️ 不包含 'Hello from E2E'")
    else:
        print(f"  产出文件: ❌ {output_path} 不存在")

    # ── 清理 ──
    print("\n[清理] 停止 TaskWorker...")
    await tw.stop()

    # ── 汇总 ──
    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"  闭环测试结果 (耗时 {elapsed:.1f}s)")
    print(f"{'=' * 60}")
    print(f"  任务提交: {'✅' if task_id else '❌'}")
    print(f"  任务执行: {'✅' if final_status == 'completed' else '❌'} ({final_status})")
    print(f"  产出文件: {'✅' if output_path.exists() else '❌'}")
    print(f"  内容正确: {'✅' if output_path.exists() and 'Hello from E2E' in output_path.read_text(encoding='utf-8') else '❌'}")

    all_pass = task_id and final_status == "completed" and output_path.exists()
    if all_pass:
        print(f"\n  🎉 任务执行闭环验证通过！")
    else:
        print(f"\n  ⚠️ 闭环未完全通过，请检查上方详情")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())
