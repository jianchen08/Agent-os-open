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

# 超时控制：几十秒就退出
MAX_WAIT_SECONDS = 300
POLL_INTERVAL = 5
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

    # ── 3. 发消息让 Agent 提交任务（创建工具） ──
    tool_path = Path("config/agents/executor/test/e2e_greeting_agent.yaml")
    if tool_path.exists():
        tool_path.unlink()

    print(f"\n[3/6] 发消息给 Agent：创建工具任务...")
    print(f"  产出目标: {tool_path}")

    try:
        result = await asyncio.wait_for(
            app._engine.run(
                user_input=(
                    "请帮我创建一个短期任务，交给 general_agent 执行。\n"
                    "任务标题：E2E闭环测试-创建工具\n"
                    f"任务描述：请创建一个名为 e2e_greeting_agent 的 Agent 配置文件，"
                    f"保存到 {tool_path}。该 Agent 的功能是接收用户名字并返回问候语。"
                    "要求：config_id 为 e2e_greeting_agent，agent_type 为 specialized，"
                    "level 为 L3，category 为 test。"
                    "system_prompt 中说明核心职责是接收名字返回问候语。"
                    "tool_ids 包含 bash_execute 和 task_evaluate。"
                    "完成后使用 task_evaluate 的 auto_complete 模式完成任务。\n"
                    f"验收标准：file_check，文件 {tool_path} 必须存在且包含 'e2e_greeting_agent'。"
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

    # 检查产出文件（子 agent 在 workspace 中执行，工具文件在 workspace 目录下）
    ws_tool_path = Path(f".ai_workspaces/{task_id}") / tool_path if task_id else tool_path
    found_path = None
    for candidate in [tool_path, ws_tool_path]:
        if candidate.exists():
            found_path = candidate
            break

    if found_path:
        content = found_path.read_text(encoding="utf-8")
        has_config_id = "e2e_greeting_agent" in content
        checks["output_exists"] = True
        checks["output_correct"] = has_config_id
        print(f"  产出文件: {found_path} ({len(content)} chars)")
        print(f"  config_id 验证: {'PASS' if has_config_id else 'FAIL'}")
    else:
        checks["output_exists"] = False
        print(f"  产出文件: 不存在 ({tool_path} 或 {ws_tool_path})")

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
