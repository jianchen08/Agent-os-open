#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Agent OS 任务执行闭环测试 — 验证端到端任务产出。

完整闭环链路：
  用户发消息 → LLM 调用 task_submit → TaskWorker 拾取 → general_agent 执行 → 产出文件 → completed

不使用 Mock，直接复用 CLIApplication，与真实启动完全一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

OUTPUT_DIR = Path("data/tasks/e2e_output")

# 超时控制：几十秒就退出
MAX_WAIT_SECONDS = 30
POLL_INTERVAL = 2
ENGINE_TIMEOUT = 60

log = logging.getLogger("closed_loop")


async def main() -> bool:
    print("=" * 60)
    print("  任务执行闭环测试")
    print("  用户发消息 → task_submit → TaskWorker → 产出 → completed")
    print("=" * 60)

    import os
    os.chdir(_PROJECT_ROOT)

    start_time = time.time()
    all_pass = True

    # ── 1. 初始化 CLIApplication ──
    print("\n[1/6] 初始化 CLIApplication...")
    from channels.cli.cli_main import CLIApplication
    app = CLIApplication()
    app.setup_pipeline()
    svc_count = len(app._services)
    print(f"  服务数: {svc_count}")
    if svc_count < 5:
        print("  [WARN] 服务数偏少，管道可能不完整")

    # ── 2. 启动 TaskWorker ──
    print("\n[2/6] 启动 TaskWorker...")
    tw = getattr(app, "_task_worker", None)
    if tw and hasattr(tw, "start"):
        await tw.start()
        print("  TaskWorker 启动成功")
    else:
        print("  [FAIL] 无 TaskWorker，无法执行后台任务")
        return False

    task_service = app._services.get("task_service")
    if not task_service:
        print("  [FAIL] 无 task_service")
        return False

    # ── 3. 发消息让 Agent 提交任务 ──
    output_path = OUTPUT_DIR / "hello.txt"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    print(f"\n[3/6] 发消息给 Agent：创建写文件任务...")
    print(f"  产出目标: {output_path}")

    try:
        result = await asyncio.wait_for(
            app._engine.run(
                user_input=(
                    "请帮我创建一个短期任务，交给 general_agent 执行。\n"
                    "任务标题：E2E闭环测试-写文件\n"
                    f"任务描述：请在文件 {output_path} 中写入一行文字："
                    "'Hello from E2E closed loop test'，"
                    "然后使用 task_evaluate 的 auto_complete 模式完成任务。\n"
                    f"验收标准：file_check，文件 {output_path} 必须存在且包含 'Hello from E2E'。"
                ),
                agent_config=app._agent_config,
                conversation_history=None,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            ),
            timeout=ENGINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        print(f"  [TIMEOUT] L1 引擎执行超过 {ENGINE_TIMEOUT}s")
        await tw.stop()
        return False

    elapsed_l1 = time.time() - start_time
    iterations = result.get("iteration", 0)
    print(f"  L1 完成: {elapsed_l1:.1f}s, {iterations} iterations")

    # ── 4. 提取 task_id ──
    task_id = None
    for tr in result.get("tool_results", []):
        if tr.get("tool_name") == "task_submit" and tr.get("success"):
            data = tr.get("data", {})
            task_id = data.get("task_id") or data.get("output", {}).get("task_id", "")
            print(f"  task_submit 返回: {json.dumps(data, ensure_ascii=False, default=str)[:200]}")
            break

    if not task_id:
        all_tasks = task_service.list_all()
        recent = [t for t in all_tasks if "E2E闭环" in getattr(t, "title", "")]
        if recent:
            recent.sort(key=lambda t: getattr(t, "created_at", ""), reverse=True)
            task_id = recent[0].id
            print(f"  从 TaskService 找到任务: {task_id}")

    if not task_id:
        raw = str(result.get("raw_result", ""))
        print(f"  [FAIL] 未找到任务 ID")
        print(f"  raw_result: {raw[:300]}")
        await tw.stop()
        return False

    print(f"  任务 ID: {task_id}")

    # ── 5. 等待任务完成 ──
    print(f"\n[4/6] 等待 TaskWorker 执行 (最多 {MAX_WAIT_SECONDS}s)...")
    from tasks.types import TaskStatus

    final_status = "unknown"
    elapsed_wait = 0

    while elapsed_wait < MAX_WAIT_SECONDS:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed_wait += POLL_INTERVAL

        task = task_service.get_task(task_id)
        if task is None:
            print(f"  [WARN] 任务 {task_id} 不存在")
            break

        status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
        if status_val in ("completed", "failed", "cancelled"):
            final_status = status_val
            print(f"  终态: {status_val} (等了 {elapsed_wait}s)")
            break
        elif elapsed_wait % 6 == 0:
            print(f"  ... 状态: {status_val} ({elapsed_wait}s)")

    if final_status == "unknown":
        task = task_service.get_task(task_id)
        if task:
            final_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        print(f"  超时, 最终状态: {final_status}")

    # ── 6. 检查任务数据 ──
    print(f"\n[5/6] 检查任务数据和产出...")

    task = task_service.get_task(task_id)
    checks: dict[str, bool] = {}

    if task:
        print(f"  标题: {task.title}")
        print(f"  状态: {final_status}")
        print(f"  结果: {str(getattr(task, 'result', None))[:200]}")
        print(f"  错误: {str(getattr(task, 'error', None))[:200]}")
        checks["task_exists"] = True
        checks["task_completed"] = final_status == "completed"

        # 检查任务 YAML 文件
        import yaml
        tree_dir = Path("data/tasks") / f"tree_{task_id}"
        yaml_file = tree_dir / f"{task_id}.yaml"
        if yaml_file.exists():
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            checks["yaml_exists"] = True
            checks["yaml_status_match"] = data.get("status") == final_status
            print(f"  YAML: {yaml_file} (status={data.get('status')})")
        else:
            checks["yaml_exists"] = False
            print(f"  YAML: 不存在 ({yaml_file})")
    else:
        checks["task_exists"] = False
        print("  [FAIL] 任务不存在")

    # 检查产出文件
    if output_path.exists():
        content = output_path.read_text(encoding="utf-8")
        has_hello = "Hello from E2E" in content
        checks["output_exists"] = True
        checks["output_correct"] = has_hello
        print(f"  产出文件: 存在 ({len(content)} chars)")
        print(f"  内容: {content[:100]}")
        print(f"  内容验证: {'PASS' if has_hello else 'FAIL'}")
    else:
        checks["output_exists"] = False
        print(f"  产出文件: 不存在 ({output_path})")

    # ── 7. 检查执行记录和日志 ──
    print(f"\n[6/6] 检查执行记录和日志...")

    # 执行记录
    record_storage = app._services.get("execution_record_storage")
    if record_storage:
        try:
            records_dir = Path("data/pipelines")
            if records_dir.exists():
                jsonl_files = list(records_dir.glob("*.jsonl"))
                print(f"  执行记录目录: {records_dir} ({len(jsonl_files)} 个 .jsonl)")
                if jsonl_files:
                    latest = max(jsonl_files, key=lambda f: f.stat().st_mtime)
                    if latest.stat().st_mtime > start_time:
                        checks["execution_record"] = True
                        lines = latest.read_text(encoding="utf-8").strip().split("\n")
                        print(f"  最新记录: {latest.name} ({len(lines)} 行)")
                    else:
                        checks["execution_record"] = False
                        print(f"  无本次测试的执行记录")
            else:
                checks["execution_record"] = False
                print(f"  执行记录目录不存在")
        except Exception as e:
            checks["execution_record"] = False
            print(f"  检查执行记录失败: {e}")
    else:
        checks["execution_record"] = False
        print(f"  execution_record_storage 不可用")

    # 日志文件
    log_file = Path("logs/agent_os.log")
    if log_file.exists():
        checks["log_exists"] = True
        log_size = log_file.stat().st_size
        print(f"  日志文件: {log_file} ({log_size} bytes)")
        # 检查有无 ERROR
        try:
            log_content = log_file.read_text(encoding="utf-8")
            error_lines = [l for l in log_content.split("\n") if "ERROR" in l]
            recent_errors = [l for l in error_lines if "2026-04-24" in l]
            if recent_errors:
                print(f"  最近 ERROR 数: {len(recent_errors)}")
                for el in recent_errors[-3:]:
                    print(f"    {el[:150]}")
            else:
                print(f"  日志无近期 ERROR")
        except Exception:
            pass
    else:
        checks["log_exists"] = False
        print(f"  日志文件不存在")

    # ── 停止 TaskWorker ──
    print("\n[清理] 停止 TaskWorker...")
    await tw.stop()

    # ── 汇总 ──
    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"  闭环测试结果 (耗时 {elapsed:.1f}s)")
    print(f"{'=' * 60}")

    for name, passed in checks.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {name}")

    all_pass = all(checks.values())

    if all_pass:
        print(f"\n  全部通过！")
    else:
        failed = [k for k, v in checks.items() if not v]
        print(f"\n  未通过: {failed}")

    return all_pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
