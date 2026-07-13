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

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# 超时控制：资源生成流程涉及多级 Agent，需要更长等待
MAX_WAIT_SECONDS = 600
POLL_INTERVAL = 10
ENGINE_TIMEOUT = 120

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
    from channels.cli.cli_main import CLIApplication, setup_logging
    setup_logging(debug=True)
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

    # ── 3. 发消息让 Agent 创建工具 ──
    tool_file = "e2e_greeting"
    tool_py_path = Path(f"src/tools/builtin/{tool_file}.py")
    tool_test_path = Path(f"src/tools/builtin/test_{tool_file}.py")

    print(f"\n[3/6] 发消息给 Agent：创建工具...")
    print(f"  期望产出: {tool_py_path}")

    try:
        result = await asyncio.wait_for(
            app._engine.run(
                user_input=(
                    "我需要一个工具：e2e_greeting，功能是接收一个名字参数，"
                    "返回个性化的问候语。比如传入 name='Alice' 返回 '你好，Alice！很高兴认识你'。"
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

    # 检查产出文件（资源生成流程产出在工作空间或项目目录）
    found_py = None
    found_test = None
    search_dirs = [Path(".")] + list(Path(".ai_workspaces").iterdir()) if Path(".ai_workspaces").exists() else [Path(".")]

    for base in search_dirs:
        if not base.is_dir():
            continue
        candidate_py = base / tool_py_path
        candidate_test = base / tool_test_path
        if candidate_py.exists():
            found_py = candidate_py
        if candidate_test.exists():
            found_test = candidate_test

    if found_py:
        content = found_py.read_text(encoding="utf-8")
        has_greeting = "greeting" in content.lower() or "e2e_greeting" in content
        checks["output_exists"] = True
        checks["output_correct"] = has_greeting
        print(f"  工具代码: {found_py} ({len(content)} chars)")
        print(f"  内容验证: {'PASS' if has_greeting else 'FAIL'}")
    else:
        checks["output_exists"] = False
        print(f"  工具代码: 不存在 ({tool_py_path})")

    if found_test:
        checks["test_exists"] = True
        print(f"  测试文件: {found_test}")
    else:
        checks["test_exists"] = False
        print(f"  测试文件: 不存在 ({tool_test_path})")

    # ── 7. 检查执行记录和日志 ──
    print(f"\n[6/6] 检查执行记录和日志...")

    # 执行记录（信息性检查，不阻塞测试结果）
    record_storage = app._services.get("execution_record_storage")
    if record_storage:
        try:
            records_dir = Path("data/pipelines")
            if records_dir.exists():
                yaml_files = list(records_dir.glob("*.yaml"))
                print(f"  执行记录目录: {records_dir} ({len(yaml_files)} 个 .yaml)")
                if yaml_files:
                    latest = max(yaml_files, key=lambda f: f.stat().st_mtime)
                    if latest.stat().st_mtime > start_time:
                        content = latest.read_text(encoding="utf-8")
                        record_count = content.count("record_id:")
                        print(f"  最新记录: {latest.name} ({record_count} 条记录)")
                    else:
                        print(f"  无本次测试的执行记录")
                else:
                    print(f"  无执行记录文件")
            else:
                print(f"  执行记录目录不存在")
        except Exception as e:
            print(f"  检查执行记录失败: {e}")
    else:
        print(f"  execution_record_storage 不可用")

    # 日志文件
    log_file = Path("logs/agent_os.log")
    if log_file.exists():
        checks["log_exists"] = True
        log_size = log_file.stat().st_size
        print(f"  日志文件: {log_file} ({log_size} bytes)")
        try:
            log_content = log_file.read_text(encoding="utf-8")
            today_str = time.strftime("%Y-%m-%d")
            error_lines = [l for l in log_content.split("\n") if "ERROR" in l and today_str in l]
            if error_lines:
                print(f"  今日 ERROR 数: {len(error_lines)}")
                for el in error_lines[-3:]:
                    print(f"    {el[:150]}")
            else:
                print(f"  日志无今日 ERROR")
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
        icon = "[PASS]" if passed else "[FAIL]"
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
