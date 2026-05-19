"""容器完整闭环 E2E 测试。

验证完整业务闭环：容器创建 → 子任务执行 → 终态通知注入 → Agent 决策 → 容器完成。

测试流程：
1. 发初始消息让 L1 创建容器 + 提交子任务
2. 等待子任务全部完成（后台异步执行）
3. 子任务完成后，发"后续消息"触发 L1 收到通知
4. L1 收到通知 → 调 task_manage complete_container
5. 验证容器状态 = COMPLETED
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tasks.types import TaskStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
for name in ["httpx", "litellm", "httpcore", "asyncio", "pipeline.event_bus"]:
    logging.getLogger(name).setLevel(logging.WARNING)
logging.getLogger("pipeline.engine").setLevel(logging.WARNING)
logging.getLogger("plugins.core.llm_core").setLevel(logging.WARNING)
logging.getLogger("plugins.core.tool_core").setLevel(logging.WARNING)
logging.getLogger("infrastructure.task_worker").setLevel(logging.WARNING)
logging.getLogger("tools.builtin.task_submit").setLevel(logging.WARNING)
logging.getLogger("evaluation").setLevel(logging.WARNING)


INITIAL_MESSAGE = """
请严格按照以下顺序，一步一步执行，不要跳跃：

第一步：用 task_submit 创建长期任务容器，只创建1个，参数：
task_submit(
  target_type="container",
  goal={"title": "猜数字游戏实现", "description": "完成一个猜数字小游戏的完整开发和验证"},
  metadata={"task_scope": "long_term"}
)
拿到容器 task_id 后执行第二步。

第二步：容器创建好后，用 task_submit 提交方案准备子任务：
task_submit(
  target_type="agent",
  target_id="solution_preparation_agent",
  parent_task_id=上面容器的task_id,
  goal={"title": "方案准备", "description": "写一个简单的猜数字小游戏方案，写入 docs/solution.md。"},
  acceptance_criteria={"file_check": {"path": "docs/solution.md"}, "format_valid": {"path": "docs/solution.md", "type": "markdown"}}
)
等方案准备子任务完成后执行第三步。

第三步：方案准备完成后，用 task_submit 提交最终评估子任务：
task_submit(
  target_type="agent",
  target_id="solution_preparation_agent",
  parent_task_id=同一个容器task_id,
  goal={"title": "最终评估", "description": "读取 docs/solution.md，用 task_evaluate 验证文件是否符合验收标准，然后用 file_write 把评估结论写入 docs/eval_report.md。"},
  acceptance_criteria={"file_check": {"path": "docs/eval_report.md"}, "format_valid": {"path": "docs/eval_report.md", "type": "markdown"}}
)
等最终评估子任务完成后执行第四步。

第四步：最终评估子任务完成后，立即用 task_manage 标记容器完成：
task_manage(action="complete_container", task_id=同一个容器task_id)

方案准备阶段不要调用 human_interaction。完成后报告容器进度和所有子任务状态。
"""

TRIGGER_MESSAGE = "上面容器的所有子任务已完成，请立即调用 task_manage complete_container 完成容器。"
CONFIRM_MESSAGE = "好的，继续。"
CANCEL_MESSAGE = "取消当前操作。"


async def _get_all_tasks(task_service) -> list:
    statuses = [
        TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.EVALUATING,
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED,
    ]
    all_tasks = []
    for s in statuses:
        all_tasks.extend(task_service.list_by_status(s))
    return all_tasks


async def _find_latest_container(task_service, after_time: float | None = None, exclude_ids: set | None = None) -> tuple | None:
    all_tasks = await _get_all_tasks(task_service)
    containers = [
        t for t in all_tasks
        if t.metadata and t.metadata.get("task_scope") == "long_term"
        and not t.parent_task_id
        and t.status != TaskStatus.FAILED
    ]
    if exclude_ids:
        containers = [c for c in containers if c.id not in exclude_ids]
    if after_time is not None:
        after_dt = datetime.fromtimestamp(after_time).strftime("%Y-%m-%dT%H:%M")
        filtered = [c for c in containers if c.created_at and c.created_at >= after_dt]
        if filtered:
            containers = filtered
    if not containers:
        return None
    containers.sort(key=lambda c: c.created_at or "", reverse=True)
    return containers[0]


async def _wait_subtasks_done(task_service, container_id: str, timeout: int = 600) -> bool:
    interval = 15
    for _ in range(timeout // interval):
        await asyncio.sleep(interval)
        subtasks = task_service.list_subtasks(container_id)
        completed = sum(1 for s in subtasks if s.status == TaskStatus.COMPLETED)
        failed = sum(1 for s in subtasks if s.status == TaskStatus.FAILED)
        total = len(subtasks)
        print(f"  [{completed}/{total} completed, {failed} failed]", flush=True)
        if total > 0 and completed + failed == total:
            return True
    return False


async def main():
    from channels.cli.cli_main import CLIApplication

    t0 = time.time()

    print("=" * 60, flush=True)
    print("PHASE 1: INITIALIZE", flush=True)
    print("=" * 60, flush=True)

    app = CLIApplication()
    app.setup_pipeline()

    tw = getattr(app, "_task_worker", None)
    if not tw:
        print("FATAL: TaskWorker not initialized!", flush=True)
        return
    await tw.start()
    print(f"Pipeline initialized. Agent: {app._agent_config.config_id}", flush=True)

    task_service = app._services.get("task_service")
    if not task_service:
        print("FATAL: TaskService not available!", flush=True)
        await tw.stop()
        return

    pre_container_ids = set()
    for s in [TaskStatus.PENDING, TaskStatus.COMPLETED, TaskStatus.FAILED]:
        for t in task_service.list_by_status(s):
            if t.metadata and t.metadata.get("task_scope") == "long_term" and not t.parent_task_id:
                pre_container_ids.add(t.id)
    print(f"Pre-existing containers: {len(pre_container_ids)}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("PHASE 2: SEND INITIAL MESSAGE", flush=True)
    print("=" * 60, flush=True)

    try:
        result = await asyncio.wait_for(
            app._engine.run(
                user_input=INITIAL_MESSAGE,
                agent_config=app._agent_config,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            ),
            timeout=300,
        )
    except asyncio.TimeoutError:
        print("TIMEOUT: L1 initial execution exceeded 300s", flush=True)
        await tw.stop()
        return

    elapsed_l1 = time.time() - t0
    print(f"L1 initial done: {elapsed_l1:.1f}s, iterations={result.get('iteration', 0)}", flush=True)

    container = await _find_latest_container(task_service, after_time=t0, exclude_ids=pre_container_ids)
    if not container:
        print("ERROR: No container found after L1 execution!", flush=True)
        await tw.stop()
        return
    print(f"Container found: {container.id[:12]} | {container.title}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("PHASE 3: WAIT FOR SUBTASKS", flush=True)
    print("=" * 60, flush=True)

    subtasks = task_service.list_subtasks(container.id)
    print(f"Subtasks under container: {len(subtasks)}", flush=True)
    for s in subtasks:
        print(f"  {s.title} | {s.status.value}", flush=True)

    subtasks_done = await _wait_subtasks_done(task_service, container.id, timeout=600)
    if not subtasks_done:
        print("TIMEOUT: Subtasks did not complete within 600s", flush=True)

    elapsed_wait = time.time() - t0
    subtasks = task_service.list_subtasks(container.id)
    completed = [s for s in subtasks if s.status == TaskStatus.COMPLETED]
    failed = [s for s in subtasks if s.status == TaskStatus.FAILED]
    print(f"\nAfter waiting ({elapsed_wait:.0f}s): {len(completed)} completed, {len(failed)} failed", flush=True)
    for s in subtasks:
        print(f"  {s.title} | {s.status.value}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("PHASE 4: SEND TRIGGER MESSAGE (notify L1 of completion)", flush=True)
    print("=" * 60, flush=True)

    try:
        result2 = await asyncio.wait_for(
            app._engine.run(
                user_input=TRIGGER_MESSAGE,
                agent_config=app._agent_config,
                streaming=False,
                auto_approve=True,
                interaction_mode="auto",
            ),
            timeout=300,
        )
    except asyncio.TimeoutError:
        print("TIMEOUT: L1 trigger execution exceeded 300s", flush=True)
        await tw.stop()
        return

    elapsed_trigger = time.time() - t0
    print(f"L1 trigger done: {elapsed_trigger:.1f}s, iterations={result2.get('iteration', 0)}", flush=True)
    try:
        raw = str(result2.get('raw_result', '') or '')[:300]
        print(f"Raw result: {raw}", flush=True)
    except Exception:
        print(f"Raw result: <binary or unprintable>", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("PHASE 5: VERIFICATION", flush=True)
    print("=" * 60, flush=True)

    elapsed_total = time.time() - t0

    all_tasks = await _get_all_tasks(task_service)
    containers_all = [
        t for t in all_tasks
        if t.metadata and t.metadata.get("task_scope") == "long_term" and not t.parent_task_id
    ]
    print(f"\nTotal tasks: {len(all_tasks)}", flush=True)
    print(f"Container tasks: {len(containers_all)}", flush=True)

    target_container = await _find_latest_container(task_service, after_time=t0, exclude_ids=pre_container_ids)
    if target_container:
        subtasks = task_service.list_subtasks(target_container.id)
        progress = task_service.get_progress(target_container.id)
        print(f"\nTarget container: {target_container.id[:12]}...", flush=True)
        print(f"  title: {target_container.title}", flush=True)
        print(f"  status: {target_container.status.value}", flush=True)
        print(f"  progress: {progress:.1f}%", flush=True)
        print(f"  subtasks ({len(subtasks)}):", flush=True)
        for s in subtasks:
            print(f"    - {s.title} | {s.status.value}", flush=True)

        if target_container.status == TaskStatus.COMPLETED:
            print(f"\n  [PASS] CONTAINER COMPLETED -- FULL CLOSED LOOP SUCCESS!", flush=True)
        elif target_container.status == TaskStatus.PENDING:
            completed_subs = [s for s in subtasks if s.status == TaskStatus.COMPLETED]
            failed_subs = [s for s in subtasks if s.status == TaskStatus.FAILED]
            if completed_subs and not failed_subs:
                print(f"\n  [WARN] Container still PENDING but all subtasks completed.", flush=True)
                print(f"      L1 may need another turn. Sending confirm...", flush=True)
                try:
                    await asyncio.wait_for(
                        app._engine.run(
                            user_input=CONFIRM_MESSAGE,
                            agent_config=app._agent_config,
                            streaming=False,
                            auto_approve=True,
                            interaction_mode="auto",
                        ),
                        timeout=120,
                    )
                    print(f"After confirm: container status = {task_service.get_task(target_container.id).status.value}", flush=True)
                except asyncio.TimeoutError:
                    print("TIMEOUT on confirm", flush=True)
            elif failed_subs:
                print(f"\n  [FAIL] Container has failed subtasks: {[s.title for s in failed_subs]}", flush=True)
            else:
                print(f"\n  [WAIT] Container still PENDING, subtasks still running", flush=True)
        else:
            print(f"\n  Container status: {target_container.status.value}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print(f"SUMMARY: {elapsed_total:.1f}s total", flush=True)
    print("=" * 60, flush=True)

    if target_container and target_container.status == TaskStatus.COMPLETED:
        print("[PASS] SUCCESS: Full container closed loop PASSED!", flush=True)
    else:
        print("[FAIL] PARTIAL: Container not completed", flush=True)

    await tw.stop()
    print("\nDone.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
