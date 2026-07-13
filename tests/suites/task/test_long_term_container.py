"""长期任务容器生命周期测试。

长期任务本质是"容器"——灵汐(L1) 创建一个父任务框架，然后往里面挂载子任务。
长期任务本身不执行任何逻辑，TaskWorker 会跳过它。

验证场景（模拟灵汐的实际操作流程）：
1. 创建长期任务容器（task_scope=long_term，不指定 target/AC）
2. 挂载方案准备子任务（target=solution_preparation_agent）
3. 挂载方案细化子任务（target=solution_refinement_agent）
4. 容器进度追踪（子任务完成 → 进度更新）
5. TaskWorker 跳过长期任务
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tasks.service import TaskService
from tasks.storage import TaskStorage
from tasks.types import TaskStatus
from tools.builtin.task_submit import TaskSubmitTool


def _make_svc(data_dir: Path | None = None) -> TaskService:
    """创建测试用 TaskService（内存或文件存储）。"""
    svc = TaskService.__new__(TaskService)
    svc._storage = TaskStorage(data_dir=data_dir)
    svc._progress = None
    svc._scheduler = None
    svc._concurrency = None
    svc._on_state_change = None
    return svc


def _make_submit_tool(svc: TaskService) -> TaskSubmitTool:
    """创建测试用 TaskSubmitTool，注入 TaskService。"""
    tool = TaskSubmitTool()
    tool._task_service = svc
    return tool


def _complete_subtask(svc: TaskService, task_id: str) -> None:
    """将子任务从 PENDING 完整流转到 COMPLETED。

    依次执行：start → move_to_evaluating → complete_evaluation(passed=True)
    """
    svc.start_task(task_id)
    svc.move_to_evaluating(task_id)
    svc.complete_evaluation(task_id, passed=True)


def _fail_subtask(svc: TaskService, task_id: str, error: str = "测试失败") -> None:
    """将子任务从 PENDING 流转到 FAILED。

    依次执行：start → fail_task
    """
    svc.start_task(task_id)
    svc.fail_task(task_id, error=error)


@pytest.fixture
def task_service() -> TaskService:
    """创建内存存储的 TaskService fixture，用于容器完成测试。"""
    return _make_svc()


# ═══════════════════════════════════════════════════════════
# 一、容器创建验证
# ═══════════════════════════════════════════════════════════


class TestLongTermContainerCreation:

    async def test_create_long_term_task_only_needs_goal_title(self) -> None:
        """长期任务创建只需 goal.title，不需要 target_type/target_id/acceptance_criteria。"""
        svc = _make_svc()
        tool = _make_submit_tool(svc)
        tool._event_bus = MagicMock()

        result = await tool.execute({
            "goal": {"title": "开发一个待办事项App"},
            "task_scope": "long_term",
        })

        assert result.success is True
        data = result.data
        assert data["task_scope"] == "long_term"
        assert data["status"] == "pending"
        assert "task_id" in data

        task = svc.get_task(data["task_id"])
        assert task is not None
        assert task.metadata.get("task_scope") == "long_term"

    async def test_long_term_task_no_parent_task_id(self) -> None:
        """长期任务不能有 parent_task_id。"""
        tool = TaskSubmitTool()
        valid = tool._validate_parent_task_id(
            parent_agent_level=1,
            parent_task_id="some_id",
            task_scope="long_term",
        )
        assert valid is False

    async def test_long_term_task_only_l1_can_submit(self) -> None:
        """长期任务只能由 L1 Agent 提交。"""
        svc = _make_svc()
        tool = _make_submit_tool(svc)

        result = await tool.execute({
            "goal": {"title": "测试任务"},
            "task_scope": "long_term",
            "parent_agent_level": 2,
        })

        assert result.success is False
        assert result.error_code == "L2_CANNOT_SUBMIT_LONG_TERM"

    async def test_long_term_task_default_scope_is_short_term(self) -> None:
        """不指定 task_scope 时默认为 short_term，需要 target_type 和 AC。"""
        svc = _make_svc()
        tool = _make_submit_tool(svc)

        result = await tool.execute({
            "goal": {"title": "短期任务"},
        })

        assert result.success is False
        assert "target_type" in result.error.lower() or "MISSING_TARGET_TYPE" in result.error_code


# ═══════════════════════════════════════════════════════════
# 二、子任务挂载与组织
# ═══════════════════════════════════════════════════════════


class TestSubtaskMounting:

    async def test_mount_preparation_subtask(self, tmp_path: Path) -> None:
        """挂载方案准备子任务，parent_task_id 正确关联容器。"""
        svc = _make_svc(data_dir=tmp_path)

        container = svc.create_task(
            title="开发待办事项App",
            metadata={"task_scope": "long_term"},
        )

        subtask = svc.create_task(
            title="方案准备：调研需求并形成初步方案",
            parent_task_id=container.id,
            metadata={
                "target_id": "solution_preparation_agent",
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "docs/solution.md"}},
                },
            },
        )

        children = svc.list_subtasks(container.id)
        assert len(children) == 1
        assert children[0].id == subtask.id
        assert children[0].parent_task_id == container.id

    async def test_mount_refinement_subtask(self, tmp_path: Path) -> None:
        """挂载方案细化子任务。"""
        svc = _make_svc(data_dir=tmp_path)

        container = svc.create_task(
            title="开发待办事项App",
            metadata={"task_scope": "long_term"},
        )

        prep = svc.create_task(
            title="方案准备",
            parent_task_id=container.id,
        )

        refine = svc.create_task(
            title="方案细化：将方案转化为可执行任务计划",
            parent_task_id=container.id,
            metadata={
                "target_id": "solution_refinement_agent",
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "docs/task_plan.md"}},
                },
            },
        )

        children = svc.list_subtasks(container.id)
        assert len(children) == 2
        child_ids = {c.id for c in children}
        assert prep.id in child_ids
        assert refine.id in child_ids

    async def test_container_lists_all_subtasks(self, tmp_path: Path) -> None:
        """通过容器查看所有子任务。"""
        svc = _make_svc(data_dir=tmp_path)

        container = svc.create_task(
            title="长期任务容器",
            metadata={"task_scope": "long_term"},
        )

        for i in range(5):
            svc.create_task(
                title=f"子任务{i+1}",
                parent_task_id=container.id,
            )

        children = svc.list_subtasks(container.id)
        assert len(children) == 5


# ═══════════════════════════════════════════════════════════
# 三、容器进度追踪
# ═══════════════════════════════════════════════════════════


class TestContainerProgress:

    async def test_progress_updates_as_subtasks_complete(self, tmp_path: Path) -> None:
        """3个子任务逐个完成 → 容器进度从 0% → 33% → 67% → 100%。"""
        svc = _make_svc(data_dir=tmp_path)

        container = svc.create_task(
            title="长期任务容器",
            metadata={"task_scope": "long_term"},
        )

        # 创建3个子任务，每个都带 task_role metadata
        svc.create_task(
            title="方案准备", parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        svc.create_task(
            title="方案细化", parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        svc.create_task(
            title="最终验证", parent_task_id=container.id,
            metadata={"task_role": "final_validation"},
        )

        assert svc.get_progress(container.id) == 0.0

        children = svc.list_subtasks(container.id)

        # 第1个子任务完成 → 33%
        _complete_subtask(svc, children[0].id)
        assert svc.get_progress(container.id) == pytest.approx(33.33, abs=0.01)

        # 第2个子任务完成 → 67%
        _complete_subtask(svc, children[1].id)
        assert svc.get_progress(container.id) == pytest.approx(66.67, abs=0.01)

        # 第3个子任务完成 → 100%
        _complete_subtask(svc, children[2].id)
        assert svc.get_progress(container.id) == 100.0

    async def test_partial_progress_on_subtask_failure(self, tmp_path: Path) -> None:
        """部分子任务失败 → 容器进度只计已完成的部分。"""
        svc = _make_svc(data_dir=tmp_path)

        container = svc.create_task(
            title="长期任务容器",
            metadata={"task_scope": "long_term"},
        )

        # 创建3个子任务，每个都带 task_role metadata
        svc.create_task(
            title="方案准备", parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        svc.create_task(
            title="方案细化", parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        svc.create_task(
            title="最终验证", parent_task_id=container.id,
            metadata={"task_role": "final_validation"},
        )

        children = svc.list_subtasks(container.id)

        # 第1个完成
        _complete_subtask(svc, children[0].id)

        # 第2个失败
        _fail_subtask(svc, children[1].id, error="细化失败")

        # 1/3 完成 ≈ 33.33%
        assert svc.get_progress(container.id) == pytest.approx(33.33, abs=0.01)

        failed_task = svc.get_task(children[1].id)
        assert failed_task.error == "细化失败"

    async def test_no_subtasks_zero_progress(self) -> None:
        """容器无子任务时进度为 0%。"""
        svc = _make_svc()

        container = svc.create_task(
            title="空容器",
            metadata={"task_scope": "long_term"},
        )

        assert svc.get_progress(container.id) == 0.0


# ═══════════════════════════════════════════════════════════
# 四、TaskWorker 跳过长期任务
# ═══════════════════════════════════════════════════════════


class TestWorkerSkipsLongTerm:

    async def test_worker_skips_long_term_task(self, tmp_path: Path) -> None:
        """TaskWorker 执行时遇到长期任务直接 return，不启动管道。"""
        from infrastructure.task_worker import TaskWorker

        svc = _make_svc(data_dir=tmp_path)
        long_task = svc.create_task(
            title="长期任务",
            metadata={"task_scope": "long_term"},
        )
        svc.start_task(long_task.id)

        services = {"task_service": svc}
        worker = TaskWorker(
            task_service=svc,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services=services,
            event_bus=None,
        )

        executed = False
        original_execute = worker._execute_background_task

        async def spy_execute(task_data, ctx=None):
            nonlocal executed
            executed = True
            return await original_execute(task_data, ctx or TaskExecutionContext(task_data.get("task_id", "")))

        worker._execute_background_task = spy_execute
        from infrastructure.task_context import TaskExecutionContext
        await worker._execute_background_task({
            "task_id": long_task.id,
        })

        assert executed is True
        task = svc.get_task(long_task.id)
        status_str = task.status if isinstance(task.status, str) else task.status.value
        assert status_str == "running", "长期任务状态不应被 Worker 改变"

    async def test_worker_recovery_skips_long_term(self, tmp_path: Path) -> None:
        """Worker 启动恢复时跳过长期任务，不 reset_to_pending。"""
        from infrastructure.task_worker import TaskWorker

        svc = _make_svc(data_dir=tmp_path)

        svc.create_task(
            title="短期任务",
            metadata={"task_scope": "short_term"},
        )
        long_task = svc.create_task(
            title="长期任务",
            metadata={"task_scope": "long_term"},
        )

        svc.start_task("short_task" if False else svc.list_by_status(TaskStatus.PENDING)[0].id)
        svc.start_task(long_task.id)

        services = {"task_service": svc}
        worker = TaskWorker(
            task_service=svc,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services=services,
            event_bus=None,
        )

        await worker._recover_running_tasks()

        short_tasks = [t for t in svc.list_by_status(TaskStatus.RUNNING) if t.id != long_task.id]
        long_task_check = svc.get_task(long_task.id)

        for t in short_tasks:
            status_str = t.status if isinstance(t.status, str) else t.status.value
            assert status_str == "pending", f"短期任务 {t.id} 应被恢复为 pending"

        long_status = long_task_check.status if isinstance(long_task_check.status, str) else long_task_check.status.value
        assert long_status == "running", "长期任务不应被 reset_to_pending"


# ═══════════════════════════════════════════════════════════
# 五、完整生命周期（模拟灵汐的实际操作流程）
# ═══════════════════════════════════════════════════════════


class TestLongTermFullLifecycle:

    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        """完整生命周期：创建容器 → 方案准备 → 方案细化 → 最终验证 → 容器完成（由主 Agent 标记）。

        模拟灵汐的实际操作：
        1. task_submit(task_scope="long_term") → 创建容器
        2. task_submit(target=solution_preparation_agent, parent_task_id=容器)
        3. task_submit(target=solution_refinement_agent, parent_task_id=容器)
        4. task_submit(target=final_validation_agent, parent_task_id=容器)
        5. 方案准备完成 → 进度 33%
        6. 方案细化完成 → 进度 67%
        7. 最终验证完成 → 进度 100%，主 Agent 通过 task_manage 标记容器完成
        """
        svc = _make_svc(data_dir=tmp_path)

        # ── 步骤1：灵汐创建长期任务容器 ──
        container = svc.create_task(
            title="开发一个待办事项App",
            description="一个支持任务增删改查的待办事项应用",
            metadata={"task_scope": "long_term"},
        )
        assert container.status == TaskStatus.PENDING
        assert container.metadata["task_scope"] == "long_term"

        # ── 步骤2：灵汐派发方案准备子任务 ──
        prep_task = svc.create_task(
            title="方案准备：调研需求并形成初步方案",
            description="与用户讨论需求，调研技术方案，输出 docs/solution.md",
            parent_task_id=container.id,
            metadata={
                "target_id": "solution_preparation_agent",
                "task_role": "solution_preparation",
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "docs/solution.md"}},
                    "semantic_check": {"input_params": {}},
                },
                "task_scope": "short_term",
            },
        )

        # ── 步骤3：灵汐派发方案细化子任务 ──
        refine_task = svc.create_task(
            title="方案细化：将方案转化为可执行任务计划",
            description="读取方案文档，细化为可执行任务链，输出 docs/task_plan.md",
            parent_task_id=container.id,
            metadata={
                "target_id": "solution_refinement_agent",
                "task_role": "solution_refinement",
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "docs/task_plan.md"}},
                    "semantic_check": {"input_params": {}},
                },
                "task_scope": "short_term",
            },
        )

        # ── 步骤4：灵汐派发最终验证子任务 ──
        validate_task = svc.create_task(
            title="最终验证：验证方案完整性和可行性",
            description="对方案和任务计划进行最终审查和验证",
            parent_task_id=container.id,
            metadata={
                "target_id": "final_validation_agent",
                "task_role": "final_validation",
                "acceptance_criteria": {
                    "semantic_check": {"input_params": {}},
                },
                "task_scope": "short_term",
            },
        )

        # ── 验证容器结构 ──
        children = svc.list_subtasks(container.id)
        assert len(children) == 3
        child_titles = {c.title for c in children}
        assert "方案准备：调研需求并形成初步方案" in child_titles
        assert "方案细化：将方案转化为可执行任务计划" in child_titles
        assert "最终验证：验证方案完整性和可行性" in child_titles

        # ── 步骤5：方案准备完成 → 进度 33% ──
        _complete_subtask(svc, prep_task.id)
        assert svc.get_progress(container.id) == pytest.approx(33.33, abs=0.01)

        # ── 步骤6：方案细化完成 → 进度 67% ──
        _complete_subtask(svc, refine_task.id)
        assert svc.get_progress(container.id) == pytest.approx(66.67, abs=0.01)

        # ── 步骤7：最终验证完成 → 进度 100% ──
        _complete_subtask(svc, validate_task.id)
        assert svc.get_progress(container.id) == 100.0

        # ── 最终验证：所有子任务已完成 ──
        prep_check = svc.get_task(prep_task.id)
        refine_check = svc.get_task(refine_task.id)
        validate_check = svc.get_task(validate_task.id)
        assert prep_check.status == TaskStatus.COMPLETED
        assert refine_check.status == TaskStatus.COMPLETED
        assert validate_check.status == TaskStatus.COMPLETED

        # ── 容器完成验证（模拟灵汐通过 task_manage change status=completed 操作）──
        svc._transition_with_callback(container, TaskStatus.COMPLETED)
        container.completed_at = datetime.now().isoformat()
        svc._storage.save(container)
        container_check = svc.get_task(container.id)
        assert container_check.status == TaskStatus.COMPLETED


# ═══════════════════════════════════════════════════════════
# 六、容器手动完成（主 Agent 标记）
# ═══════════════════════════════════════════════════════════


class TestContainerManualCompletion:
    """容器完成由主 Agent 通过 task_manage 主动标记。"""

    def test_container_complete_via_transition(self, task_service: TaskService) -> None:
        """所有子任务完成后，主 Agent 标记容器为 COMPLETED。"""
        container = task_service.create_task(
            title="容器", metadata={"task_scope": "long_term"},
        )
        sub1 = task_service.create_task(
            title="方案准备", parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        sub2 = task_service.create_task(
            title="方案细化", parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        sub3 = task_service.create_task(
            title="最终验证", parent_task_id=container.id,
            metadata={"task_role": "final_validation"},
        )
        for sub in [sub1, sub2, sub3]:
            _complete_subtask(task_service, sub.id)

        # 模拟主 Agent 通过 task_manage change status=completed 操作
        task_service._transition_with_callback(container, TaskStatus.COMPLETED)
        container.completed_at = datetime.now().isoformat()
        task_service._storage.save(container)

        container = task_service.get_task(container.id)
        assert container.status == TaskStatus.COMPLETED

    def test_container_fail_via_transition(self, task_service: TaskService) -> None:
        """子任务失败后，主 Agent 标记容器为 FAILED。"""
        container = task_service.create_task(
            title="容器", metadata={"task_scope": "long_term"},
        )
        sub1 = task_service.create_task(
            title="方案准备", parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        sub2 = task_service.create_task(
            title="方案细化", parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        _complete_subtask(task_service, sub1.id)
        _fail_subtask(task_service, sub2.id, error="细化失败")

        # 模拟主 Agent 通过 task_manage change status=failed 操作
        task_service._transition_with_callback(container, TaskStatus.FAILED)
        container.error = "子任务失败，容器标记失败"
        task_service._storage.save(container)

        container = task_service.get_task(container.id)
        assert container.status == TaskStatus.FAILED

    def test_container_stays_pending_if_not_all_done(self, task_service: TaskService) -> None:
        """子任务未全部完成时，容器保持 PENDING。"""
        container = task_service.create_task(
            title="容器", metadata={"task_scope": "long_term"},
        )
        sub1 = task_service.create_task(
            title="方案准备", parent_task_id=container.id,
            metadata={"task_role": "solution_preparation"},
        )
        task_service.create_task(
            title="方案细化", parent_task_id=container.id,
            metadata={"task_role": "solution_refinement"},
        )
        # 只完成第1个
        _complete_subtask(task_service, sub1.id)

        container = task_service.get_task(container.id)
        assert container.status == TaskStatus.PENDING
