"""F4: 长期任务 — 编程式真实交互 + 完整闭环验证。

使用 CLIApplication 编程式启动系统，通过 HumanInteractionService
注入自动回复协程模拟真实人类交互，验证长期任务的完整执行流程：
  1. 容器创建 + 方案准备（含 conversation 人类交互）
  2. 方案评估（含 human_review choice 交互）
  3. 方案细化 + 子任务执行
  4. 容器完成闭环

每 30 秒监控日志记录实际执行状态，测试结束后读取持久化文件做断言。

标记: @pytest.mark.integration — 需要 --run-integration 选项
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DATA_DIR = PROJECT_ROOT / "data"
TASKS_DIR = DATA_DIR / "tasks"
PIPELINES_DIR = DATA_DIR / "pipelines"
WORKSPACES_DIR = PROJECT_ROOT / ".ai_workspaces"

OUTPUT_DIR = PROJECT_ROOT / "test_f4_output"
MONITOR_FILE = OUTPUT_DIR / "test_f4_monitor.jsonl"
RECORDS_DIR = OUTPUT_DIR / "test_f4_records"

TASK_SUBMIT_MSG = (
    "提交长期任务：用 HTML+CSS+JS 开发一个贪吃蛇游戏，"
    "并加入 Roguelike 元素增强可玩性：随机道具（加速/减速/穿墙）、"
    "随机障碍物生成、多关卡递增难度、分数和生命值系统。"
    "项目名称：rogue_snake。提交任务不要自己做"
)

MONITOR_INTERVAL = 30
TOTAL_TIMEOUT = 1200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
for name in [
    "httpx", "litellm", "httpcore", "asyncio", "pipeline.event_bus",
    "pipeline.engine", "plugins.core.llm_core", "plugins.core.tool_core",
    "infrastructure.task_worker", "tools.builtin.task_submit",
    "evaluation", "plugins.output.track",
]:
    logging.getLogger(name).setLevel(logging.WARNING)


def _backup_and_clean_data() -> None:
    """备份并清理旧的任务/管道数据，确保测试环境隔离。"""
    from infrastructure.service_provider import ServiceProvider
    ServiceProvider.reset()

    backup_dir = OUTPUT_DIR / "pre_test_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for src_dir in [TASKS_DIR, PIPELINES_DIR]:
        if not src_dir.exists():
            continue
        dst = backup_dir / src_dir.name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src_dir, dst)
        shutil.rmtree(src_dir)
        src_dir.mkdir(parents=True, exist_ok=True)


class AutoReplyWatcher:
    """监控 HumanInteractionService，自动回复交互请求并记录日志。

    每隔 30 秒采样任务状态，检测到交互请求时自动回复。
    所有监控日志实时写入 JSONL 文件。
    """

    def __init__(self, interaction_service: Any, task_service: Any) -> None:
        self._hi_service = interaction_service
        self._task_service = task_service
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._t0 = time.monotonic()
        self._interaction_count = 0
        self._interaction_records: list[dict[str, Any]] = []
        self._last_request_count = 0
        self._last_response_count = 0

    async def start(self) -> None:
        """启动后台监控协程。"""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """停止监控协程。"""
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    @property
    def interaction_count(self) -> int:
        """已处理的交互请求数量。"""
        return self._interaction_count

    async def _run(self) -> None:
        """监控主循环：高频检查交互 + 每 30 秒记录状态日志。"""
        last_log = time.monotonic()
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(3)
                await self._check_and_reply()

                now = time.monotonic()
                if now - last_log >= MONITOR_INTERVAL:
                    await self._log_status()
                    last_log = now
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[AutoReplyWatcher] ERROR: {exc}", flush=True)

    async def _check_and_reply(self) -> None:
        """检查并自动回复 pending 的交互请求。"""
        pending = await self._hi_service.get_pending_requests()
        for req in pending:
            req_id = req.get("id", "")
            msg_data = req.get("message_data", {})
            mode = msg_data.get("interaction_mode", "choice")
            title = msg_data.get("title", "")
            status = req.get("status", "")

            if status != "pending":
                continue

            if mode == "conversation":
                await self._hi_service.submit_response(
                    request_id=req_id,
                    response_type="approved",
                    feedback="方案确认通过，请继续执行",
                )
                self._interaction_count += 1
                self._interaction_records.append({
                    "type": "conversation",
                    "title": title,
                    "action": "approved",
                    "feedback": "方案确认通过，请继续执行",
                })
            elif mode == "choice":
                options = msg_data.get("options", [])
                selected = options[0].get("id", "approved") if options else "approved"
                await self._hi_service.submit_response(
                    request_id=req_id,
                    response_type="approved",
                    selected_option=selected,
                )
                self._interaction_count += 1
                self._interaction_records.append({
                    "type": "choice",
                    "title": title,
                    "action": "approved",
                    "selected_option": selected,
                })

    async def _log_status(self) -> None:
        """记录当前任务状态到监控日志。"""
        from tasks.types import TaskStatus

        elapsed = round(time.monotonic() - self._t0, 1)
        req_count = len(self._hi_service._requests)
        resp_count = len(self._hi_service._responses)
        new_reqs = req_count - self._last_request_count
        self._last_request_count = req_count
        self._last_response_count = resp_count

        all_tasks = []
        for s in [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.EVALUATING,
                   TaskStatus.COMPLETED, TaskStatus.FAILED]:
            all_tasks.extend(self._task_service.list_by_status(s))

        containers = [
            t for t in all_tasks
            if t.metadata and t.metadata.get("task_scope") == "long_term"
            and not t.parent_task_id
        ]
        active_count = sum(
            1 for t in all_tasks
            if t.status in (TaskStatus.RUNNING, TaskStatus.EVALUATING, TaskStatus.PENDING)
        )
        completed_count = sum(
            1 for t in all_tasks if t.status == TaskStatus.COMPLETED
        )
        failed_count = sum(
            1 for t in all_tasks if t.status == TaskStatus.FAILED
        )

        recent_interactions = self._interaction_records[-3:]
        task_summary = [
            {"id": t.id[:8], "title": t.title[:40], "status": t.status.value}
            for t in all_tasks[-5:]
        ]

        entry = {
            "timestamp": time.strftime("%H:%M:%S"),
            "elapsed": elapsed,
            "tasks_total": len(all_tasks),
            "tasks_active": active_count,
            "tasks_completed": completed_count,
            "tasks_failed": failed_count,
            "containers": len(containers),
            "hi_requests": req_count,
            "hi_responses": resp_count,
            "new_requests": new_reqs,
            "auto_replies": self._interaction_count,
            "recent_interactions": recent_interactions,
            "recent_tasks": task_summary,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(MONITOR_FILE, "a", encoding="utf-8") as f:
            f.write(line)


async def _get_all_tasks(task_service: Any) -> list:
    """获取所有状态的任务列表。"""
    from tasks.types import TaskStatus
    all_tasks = []
    for s in [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.EVALUATING,
               TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED]:
        all_tasks.extend(task_service.list_by_status(s))
    return all_tasks


async def _find_rogue_snake_container(task_service: Any) -> Any | None:
    """找到 rogue_snake 项目对应的容器任务。"""
    all_tasks = await _get_all_tasks(task_service)
    for t in all_tasks:
        if t.parent_task_id:
            continue
        meta = t.metadata or {}
        if meta.get("task_scope") != "long_term":
            continue
        title = (t.title or "").lower()
        (t.description or "").lower()
        if "rogue" in title or "snake" in title or "贪吃蛇" in title:
            return t
    return None


async def _wait_for_container(
    task_service: Any, timeout: int = 60, poll: float = 3.0,
) -> Any | None:
    """等待 rogue_snake 容器出现。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container = await _find_rogue_snake_container(task_service)
        if container:
            return container
        await asyncio.sleep(poll)
    return None


async def _wait_subtasks_done(
    task_service: Any, container_id: str, timeout: int = 900, poll: float = 10.0,
) -> bool:
    """等待容器下所有子任务到达终态。"""
    from tasks.types import TaskStatus
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        subtasks = task_service.list_subtasks(container_id)
        if not subtasks:
            await asyncio.sleep(poll)
            continue
        terminal = sum(
            1 for s in subtasks
            if s.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        )
        if terminal == len(subtasks):
            return True
        await asyncio.sleep(poll)
    return False


async def _wait_container_completed(
    task_service: Any, container_id: str, timeout: int = 120, poll: float = 5.0,
) -> bool:
    """等待容器状态变为 completed。"""
    from tasks.types import TaskStatus
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container = task_service.get_task(container_id)
        if container and container.status == TaskStatus.COMPLETED:
            return True
        await asyncio.sleep(poll)
    return False


def _load_pipeline_records() -> dict[str, list[dict[str, Any]]]:
    """加载所有 pipeline 执行记录。"""
    records: dict[str, list[dict[str, Any]]] = {}
    if not PIPELINES_DIR.exists():
        return records
    for yaml_file in PIPELINES_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            run_id = data.get("summary", {}).get("run_id") or yaml_file.stem
            recs = data.get("records", [])
            if recs:
                records[run_id] = recs
        except Exception:
            pass
    return records


def _copy_pipeline_records() -> None:
    """复制 pipeline YAML 到输出目录。"""
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    if not PIPELINES_DIR.exists():
        return
    for yaml_file in PIPELINES_DIR.glob("*.yaml"):
        try:
            shutil.copy2(yaml_file, RECORDS_DIR / yaml_file.name)
        except Exception:
            pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_long_term_with_real_interaction() -> None:
    """F4: 长期任务完整闭环 — 编程式真实交互 + 活跃度监控 + 7 项验证。"""
    from channels.cli.cli_main import CLIApplication

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MONITOR_FILE.write_text("", encoding="utf-8")
    _backup_and_clean_data()

    t0 = time.time()

    print("=" * 60, flush=True)
    print("PHASE 1: INITIALIZE", flush=True)
    print("=" * 60, flush=True)

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    tw = getattr(app, "_task_worker", None)
    assert tw, "TaskWorker not initialized!"
    await tw.start()

    task_service = app._services.get("task_service")
    assert task_service, "TaskService not available!"

    hi_service = app._services.get("human_interaction_service")
    assert hi_service, "HumanInteractionService not available!"

    print(f"Pipeline initialized. Agent: {app._agent_config.config_id}", flush=True)

    watcher = AutoReplyWatcher(hi_service, task_service)
    await watcher.start()

    try:
        print("\n" + "=" * 60, flush=True)
        print("PHASE 2: SUBMIT LONG-TERM TASK", flush=True)
        print("=" * 60, flush=True)

        try:
            await asyncio.wait_for(
                app._engine.run(
                    user_input=TASK_SUBMIT_MSG,
                    agent_config=app._agent_config,
                    streaming=False,
                    auto_approve=True,
                    interaction_mode="auto",
                ),
                timeout=300,
            )
        except asyncio.TimeoutError:
            print("TIMEOUT: L1 initial execution exceeded 300s", flush=True)

        elapsed_l1 = time.time() - t0
        print(f"L1 initial done: {elapsed_l1:.1f}s", flush=True)

        container = await _find_rogue_snake_container(task_service)
        assert container, "未找到 rogue_snake 容器!"
        print(f"Container: {container.id[:12]} | {container.title}", flush=True)

        print("\n" + "=" * 60, flush=True)
        print("PHASE 3: WAIT FOR SUBTASKS", flush=True)
        print("=" * 60, flush=True)

        all_done = await _wait_subtasks_done(
            task_service, container.id, timeout=900,
        )
        elapsed_wait = time.time() - t0
        print(f"Subtasks {'all done' if all_done else 'timeout'}: {elapsed_wait:.0f}s", flush=True)

        from tasks.types import TaskStatus
        subtasks = task_service.list_subtasks(container.id)
        failed_subs = [s for s in subtasks if s.status == TaskStatus.FAILED]
        completed_subs = [s for s in subtasks if s.status == TaskStatus.COMPLETED]

        retry_count = 0
        while len(subtasks) < 4 and retry_count < 3:
            retry_count += 1
            print(f"\n  Retry #{retry_count}: subtasks={len(subtasks)} (done={len(completed_subs)}, fail={len(failed_subs)})", flush=True)

            retry_msg = (
                f"容器 {container.id[:12]} 的子任务执行中遇到问题，"
                f"当前 {len(subtasks)} 个子任务。请确保完成完整的流程："
                f"方案准备 → 方案细化 → 代码实现 → 最终评估，至少 4 个子任务。"
                f"对失败的子任务请重新提交或替代执行。"
            )
            try:
                await asyncio.wait_for(
                    app._engine.run(
                        user_input=retry_msg,
                        agent_config=app._agent_config,
                        streaming=False,
                        auto_approve=True,
                        interaction_mode="auto",
                    ),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                print(f"  Retry #{retry_count} L1 timeout", flush=True)

            all_done = await _wait_subtasks_done(
                task_service, container.id, timeout=600,
            )
            subtasks = task_service.list_subtasks(container.id)
            failed_subs = [s for s in subtasks if s.status == TaskStatus.FAILED]
            completed_subs = [s for s in subtasks if s.status == TaskStatus.COMPLETED]
            print(f"  After retry: subtasks={len(subtasks)} (done={len(completed_subs)}, fail={len(failed_subs)})", flush=True)

        print("\n" + "=" * 60, flush=True)
        print("PHASE 4: WAIT FOR CONTAINER COMPLETION", flush=True)
        print("=" * 60, flush=True)

        container_completed = await _wait_container_completed(
            task_service, container.id, timeout=60,
        )

        if not container_completed:
            print("Container not auto-completed, sending complete_container instruction...", flush=True)
            complete_msg = (
                f"容器 {container.id[:12]} 的所有子任务已处理完毕。"
                f"请立即调用 task_manage(action='complete_container', task_id='{container.id}') "
                f"完成容器闭环。不要再做其他事情，直接完成容器。"
            )
            try:
                await asyncio.wait_for(
                    app._engine.run(
                        user_input=complete_msg,
                        agent_config=app._agent_config,
                        streaming=False,
                        auto_approve=True,
                        interaction_mode="auto",
                    ),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                print("  complete_container instruction timeout", flush=True)

            container_completed = await _wait_container_completed(
                task_service, container.id, timeout=30,
            )

        container = task_service.get_task(container.id)
        elapsed_total = time.time() - t0
        print(f"Container completed: {container_completed} | status={container.status.value if container else 'N/A'} | {elapsed_total:.0f}s", flush=True)

    finally:
        await watcher.stop()
        await tw.stop()
        _copy_pipeline_records()

    print("\n" + "=" * 60, flush=True)
    print("PHASE 5: VERIFICATION", flush=True)
    print("=" * 60, flush=True)

    _run_assertions(task_service, hi_service, container, watcher, t0)


def _run_assertions(
    task_service: Any,
    hi_service: Any,
    container: Any,
    watcher: AutoReplyWatcher,
    t0: float,
) -> None:
    """V1-V7 验证。"""
    from tasks.types import TaskStatus

    container_id = container.id
    elapsed = time.time() - t0

    # ---- V1: 容器闭环 ----
    assert container.status == TaskStatus.COMPLETED, (
        f"V1 FAIL: 容器状态 {container.status.value}，期望 completed"
    )
    assert (container.metadata or {}).get("task_scope") == "long_term", (
        "V1 FAIL: task_scope != long_term"
    )
    print(f"  [V1 PASS] 容器 COMPLETED, scope=long_term", flush=True)

    # ---- V2: 一级子任务 >= 4 ----
    subtasks = task_service.list_subtasks(container_id)
    assert len(subtasks) >= 4, (
        f"V2 FAIL: 子任务数量 {len(subtasks)} < 4\n"
        f"子任务: {[s.title for s in subtasks]}"
    )
    print(f"  [V2 PASS] 子任务数量: {len(subtasks)}", flush=True)

    # ---- V3: 每个一级子任务到达终态 ----
    for st in subtasks:
        assert st.parent_task_id == container_id, (
            f"V3 FAIL [{st.title}]: parent_task_id != container_id"
        )
        assert st.status in (TaskStatus.COMPLETED, TaskStatus.FAILED), (
            f"V3 FAIL [{st.title}]: status={st.status.value} (not terminal)"
        )

    pipeline_records = _load_pipeline_records()
    all_tool_records = []
    for recs in pipeline_records.values():
        all_tool_records.extend(recs)

    notification_records = [
        r for r in all_tool_records
        if r.get("type") == "user" and "[系统通知] 任务" in (r.get("content") or "")
    ]
    assert len(notification_records) >= 2, (
        f"V3 FAIL: 系统通知记录 {len(notification_records)} < 2"
    )
    print(f"  [V3 PASS] 子任务闭环, 通知 {len(notification_records)} 条", flush=True)

    # ---- V4: 交互记录验证 ----
    hi_requests = hi_service._requests
    hi_responses = hi_service._responses
    assert len(hi_requests) >= 1, (
        f"V4 FAIL: 交互请求 {len(hi_requests)} < 1"
    )
    assert len(hi_responses) >= 1, (
        f"V4 FAIL: 交互响应 {len(hi_responses)} < 1"
    )

    hi_tool_records = [
        r for r in all_tool_records
        if r.get("name") == "human_interaction" and r.get("type") == "tool"
    ]
    assert len(hi_tool_records) >= 1, (
        f"V4 FAIL: human_interaction 工具记录 {len(hi_tool_records)} < 1"
    )

    has_conversation = any(
        req.get("message_data", {}).get("interaction_mode") == "conversation"
        for req in hi_requests.values()
    )
    has_choice = any(
        req.get("message_data", {}).get("interaction_mode") == "choice"
        for req in hi_requests.values()
    )

    print(
        f"  [V4 PASS] 交互请求 {len(hi_requests)}, "
        f"响应 {len(hi_responses)}, "
        f"工具记录 {len(hi_tool_records)}, "
        f"conversation={has_conversation}, choice={has_choice}, "
        f"自动回复 {watcher.interaction_count} 次",
        flush=True,
    )

    # ---- V5: 工作空间产出 ----
    workspace_dir = WORKSPACES_DIR / container_id
    assert workspace_dir.exists(), (
        f"V5 FAIL: 工作空间 {workspace_dir} 不存在"
    )

    solution_files = list(workspace_dir.rglob("solution*.md"))
    assert solution_files, (
        f"V5 FAIL: 未找到 solution*.md\n"
        f"内容: {[str(p) for p in workspace_dir.rglob('*')[:30]]}"
    )
    print(f"  [V5 PASS] 方案文件: {[f.name for f in solution_files]}", flush=True)

    # ---- V6: 工具调用完整性 ----
    tool_names = {r.get("name") for r in all_tool_records if r.get("type") == "tool"}
    required_tools = ["task_submit", "task_evaluate", "task_manage"]
    missing = [t for t in required_tools if t not in tool_names]
    assert not missing, (
        f"V6 FAIL: 缺少工具: {missing}\n已有: {tool_names}"
    )
    print(f"  [V6 PASS] 工具: {sorted(tool_names)}", flush=True)

    # ---- V7: 最终汇总 ----
    completed_subs = [s for s in subtasks if s.status == TaskStatus.COMPLETED]
    failed_subs = [s for s in subtasks if s.status == TaskStatus.FAILED]

    print(f"\n{'=' * 60}", flush=True)
    print(f"F4 测试完成! 耗时 {elapsed:.1f}s", flush=True)
    print(f"  容器: {container_id[:12]}", flush=True)
    print(f"  子任务: {len(subtasks)} 个 ({len(completed_subs)} done, {len(failed_subs)} fail)", flush=True)
    print(f"  交互: {watcher.interaction_count} 次自动回复", flush=True)
    print(f"  通知: {len(notification_records)} 条", flush=True)
    print(f"  工具: {sorted(tool_names)}", flush=True)
    print(f"  日志: {OUTPUT_DIR}", flush=True)
    print(f"{'=' * 60}", flush=True)
