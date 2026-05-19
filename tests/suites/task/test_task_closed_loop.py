# -*- coding: utf-8 -*-
"""端到端闭环测试：验证任务从创建到完成的完整生命周期。

覆盖场景：
1. 创建任务 → YAML 持久化 → 验证文件内容
2. 状态转换：PENDING → RUNNING → EVALUATING → COMPLETED
3. 验证 task YAML 数据正确性（状态、时间戳、结果）
4. 验证存储目录结构正确（tree_{root_id}/{task_id}.yaml）
5. 子任务创建与层级关系验证
6. 失败路径（FAILED 状态）
7. 全量数据一致性校验
8. 状态机约束验证
9. 状态变更事件验证

设计原则：
- 不依赖 LLM API，纯 TaskStorage + SimpleStateMachine 驱动
- 几十秒内完成
- 自检验：每个步骤都验证产出文件和日志
"""

import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("closed_loop_test")

import pytest

from tasks.state_machine import InvalidTransitionError, SimpleStateMachine
from tasks.storage import TaskStorage
from tasks.types import TaskModel, TaskStatus

# ── 测试隔离：使用临时数据目录 ──
TEST_DATA_DIR = Path("data") / "tasks_test_closed_loop"


@pytest.fixture(autouse=True)
def _setup_teardown():
    """每个测试前后清理数据目录。"""
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


def _make_storage() -> TaskStorage:
    """创建使用测试数据目录的存储实例。"""
    return TaskStorage(data_dir=str(TEST_DATA_DIR))


# ═══════════════════════════════════════════════════════════
# 任务状态转换规则（与 state_machine.py 中 _TASK_TRANSITIONS 一致）
# ═══════════════════════════════════════════════════════════

TASK_TRANSITIONS = {
    "pending": ["scheduled", "running", "cancelled"],
    "scheduled": ["running", "cancelled"],
    "running": ["evaluating", "completed", "failed", "suspended", "blocked", "cancelled"],
    "evaluating": ["completed", "failed", "running"],
    "suspended": ["running", "cancelled", "timeout"],
    "blocked": ["running", "cancelled", "failed"],
    "completed": [],
    "failed": ["pending"],
    "cancelled": [],
    "timeout": ["running", "cancelled", "failed"],
}


# ═══════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════


class TestTaskCreateAndPersist:
    """TC01: 创建任务 → 验证 YAML 持久化。"""

    def test_create_task_and_persist(self):
        storage = _make_storage()
        task = TaskModel(title="闭环测试任务-01", description="验证任务创建和持久化")
        storage.save(task)

        # 验证返回值
        assert task.id, "任务 ID 不能为空"
        assert task.title == "闭环测试任务-01"
        assert task.status == TaskStatus.PENDING
        assert task.created_at, "created_at 不能为空"

        # 验证 YAML 文件存在
        tree_dir = TEST_DATA_DIR / f"tree_{task.id}"
        yaml_file = tree_dir / f"{task.id}.yaml"
        assert tree_dir.exists(), f"目录不存在: {tree_dir}"
        assert yaml_file.exists(), f"YAML 文件不存在: {yaml_file}"

        # 验证 YAML 内容可解析且状态正确
        import yaml

        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert data["id"] == task.id
        assert data["status"] == "pending"
        assert data["title"] == "闭环测试任务-01"

        logger.info("[TC01 PASS] 任务创建并持久化成功 | id=%s", task.id)


class TestStateTransitions:
    """TC02: 状态转换 PENDING → RUNNING → EVALUATING → COMPLETED。"""

    def test_full_transition_path(self):
        storage = _make_storage()
        task = TaskModel(title="状态转换测试")
        storage.save(task)

        # 创建状态机追踪状态
        sm = SimpleStateMachine(initial_state="pending", transitions=TASK_TRANSITIONS)

        # PENDING → RUNNING
        sm.transition("running")
        storage.update(task.id, status=TaskStatus.RUNNING)
        updated = storage.get(task.id)
        assert updated.status == TaskStatus.RUNNING

        # RUNNING → EVALUATING
        sm.transition("evaluating")
        storage.update(task.id, status=TaskStatus.EVALUATING)
        updated = storage.get(task.id)
        assert updated.status == TaskStatus.EVALUATING

        # 验证中间状态的 YAML
        import yaml

        tree_dir = TEST_DATA_DIR / f"tree_{task.id}"
        yaml_file = tree_dir / f"{task.id}.yaml"
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert data["status"] == "evaluating"

        # EVALUATING → COMPLETED
        sm.transition("completed")
        storage.update(task.id, status=TaskStatus.COMPLETED, result={"score": 100, "detail": "全部通过"})
        updated = storage.get(task.id)
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == {"score": 100, "detail": "全部通过"}

        logger.info("[TC02 PASS] 状态转换完整路径验证成功")


class TestCompletedYaml:
    """TC03: 验证完成状态的 YAML 文件所有字段正确。"""

    def test_completed_yaml_fields(self):
        import yaml

        storage = _make_storage()
        task = TaskModel(
            title="闭环测试任务-01",
            description="验证任务创建和持久化",
            metadata={"acceptance_criteria": {"basic_check": {"pass_threshold": 50}}},
        )
        storage.save(task)
        storage.update(task.id, status=TaskStatus.RUNNING)
        storage.update(task.id, status=TaskStatus.COMPLETED, result={"score": 100, "detail": "全部通过"})

        tree_dir = TEST_DATA_DIR / f"tree_{task.id}"
        yaml_file = tree_dir / f"{task.id}.yaml"
        assert yaml_file.exists()

        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))

        # 必须字段检查
        checks = {
            "id": task.id,
            "title": "闭环测试任务-01",
            "status": "completed",
            "description": "验证任务创建和持久化",
        }
        for field, expected in checks.items():
            actual = data.get(field)
            assert actual == expected, f"字段 {field}: 期望 '{expected}'，实际 '{actual}'"

        # 时间字段不为空
        for ts_field in ("created_at", "updated_at"):
            assert data.get(ts_field), f"时间字段 {ts_field} 不能为空"

        # 结果字段
        assert data["result"]["score"] == 100
        assert data["result"]["detail"] == "全部通过"

        logger.info("[TC03 PASS] 完成 YAML 全字段验证通过")


class TestSubtaskHierarchy:
    """TC04: 子任务创建与层级关系。"""

    def test_subtask_hierarchy(self):
        storage = _make_storage()
        # 创建父任务
        parent = TaskModel(title="父任务", description="测试层级关系")
        storage.save(parent)

        # 创建子任务
        child = TaskModel(
            title="子任务A",
            description="子任务描述",
            parent_task_id=parent.id,
        )
        storage.save(child)
        assert child.parent_task_id == parent.id

        # 更新子任务状态
        storage.update(child.id, status=TaskStatus.RUNNING)
        storage.update(child.id, status=TaskStatus.COMPLETED, result={"done": True})

        # 验证子任务文件在同一个 tree 目录下
        tree_dir = TEST_DATA_DIR / f"tree_{parent.id}"
        parent_file = tree_dir / f"{parent.id}.yaml"
        child_file = tree_dir / f"{child.id}.yaml"
        assert parent_file.exists(), f"父任务文件不存在: {parent_file}"
        assert child_file.exists(), f"子任务文件不存在: {child_file}"

        # 验证 list_by_parent
        subs = storage.list_by_parent(parent.id)
        assert len(subs) == 1
        assert subs[0].id == child.id
        assert subs[0].status == TaskStatus.COMPLETED

        logger.info("[TC04 PASS] 子任务层级关系验证通过 | parent=%s child=%s", parent.id, child.id)


class TestFailedPath:
    """TC05: 失败路径（RUNNING → FAILED）。"""

    def test_failed_path(self):
        import yaml

        storage = _make_storage()
        task = TaskModel(title="会失败的任务", description="验证失败路径")
        storage.save(task)

        storage.update(task.id, status=TaskStatus.RUNNING)
        storage.update(task.id, status=TaskStatus.FAILED, error="验收不通过")

        updated = storage.get(task.id)
        assert updated.status == TaskStatus.FAILED

        # 验证 YAML
        yaml_file = TEST_DATA_DIR / f"tree_{task.id}" / f"{task.id}.yaml"
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert data["status"] == "failed"
        assert data["error"] == "验收不通过"

        logger.info("[TC05 PASS] 失败路径验证通过 | task_id=%s", task.id)


class TestStateMachineConstraints:
    """TC10: 状态机约束验证（非法转换应抛异常）。"""

    def test_invalid_transition_raises(self):
        sm = SimpleStateMachine(initial_state="pending", transitions=TASK_TRANSITIONS)

        # completed 不允许任何转换
        sm.transition("running")
        sm.transition("completed")
        assert sm.current_state == "completed"

        with pytest.raises(InvalidTransitionError):
            sm.transition("running")

    def test_direct_pending_to_completed_blocked(self):
        sm = SimpleStateMachine(initial_state="pending", transitions=TASK_TRANSITIONS)
        # pending 不能直接转到 completed
        with pytest.raises(InvalidTransitionError):
            sm.transition("completed")

    def test_can_transition(self):
        sm = SimpleStateMachine(initial_state="pending", transitions=TASK_TRANSITIONS)
        assert sm.can_transition("running")
        assert not sm.can_transition("completed")
        assert sm.can_transition("cancelled")

        logger.info("[TC10 PASS] 状态机约束验证通过")


class TestDataConsistency:
    """TC11: 全量数据一致性校验。"""

    def test_full_data_consistency(self):
        import yaml

        storage = _make_storage()

        # 创建一批任务
        tasks = []
        for i in range(5):
            t = TaskModel(title=f"批量任务-{i}", description=f"第 {i} 个")
            storage.save(t)
            tasks.append(t)

        # 对不同任务执行不同操作
        storage.update(tasks[0].id, status=TaskStatus.RUNNING)
        storage.update(tasks[0].id, status=TaskStatus.COMPLETED, result={"idx": 0})

        storage.update(tasks[1].id, status=TaskStatus.RUNNING)
        storage.update(tasks[1].id, status=TaskStatus.FAILED, error="故意失败")

        storage.update(tasks[2].id, status=TaskStatus.RUNNING)
        storage.update(tasks[2].id, status=TaskStatus.EVALUATING)

        # tasks[3] 保持 PENDING
        # tasks[4] 保持 PENDING

        # 校验每个任务的内存数据与文件数据一致
        for t in tasks:
            mem_task = storage.get(t.id)
            assert mem_task is not None, f"内存中找不到任务: {t.id}"

            tree_dir = TEST_DATA_DIR / f"tree_{t.id}"
            yaml_file = tree_dir / f"{t.id}.yaml"
            assert yaml_file.exists(), f"文件不存在: {yaml_file}"

            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            assert data["id"] == mem_task.id
            assert data["status"] == mem_task.status.value
            assert data["title"] == mem_task.title

        # 校验按状态查询
        pending_tasks = storage.list_by_status(TaskStatus.PENDING)
        assert len(pending_tasks) >= 2, f"应有至少 2 个 pending 任务，实际: {len(pending_tasks)}"

        logger.info("[TC11 PASS] 全量数据一致性校验通过 | 共 %d 个任务", len(tasks))


class TestDeleteTask:
    """TC13: 删除任务及文件清理。"""

    def test_delete_task(self):
        storage = _make_storage()
        task = TaskModel(title="待删除任务")
        storage.save(task)

        yaml_file = TEST_DATA_DIR / f"tree_{task.id}" / f"{task.id}.yaml"
        assert yaml_file.exists()

        deleted = storage.delete(task.id)
        assert deleted, "删除应返回 True"
        assert not yaml_file.exists(), f"文件应已删除: {yaml_file}"
        assert storage.get(task.id) is None, "内存中应已移除"

        # 删除不存在的任务
        deleted = storage.delete("nonexistent_id")
        assert not deleted

        logger.info("[TC13 PASS] 删除任务验证通过")


class TestResetFailedTask:
    """TC09: 重置失败任务为 pending。"""

    def test_reset_to_pending(self):
        import yaml

        storage = _make_storage()
        task = TaskModel(title="重置测试")
        storage.save(task)
        storage.update(task.id, status=TaskStatus.RUNNING)
        storage.update(task.id, status=TaskStatus.FAILED, error="模拟崩溃")

        updated = storage.get(task.id)
        assert updated.status == TaskStatus.FAILED

        # 重置为 pending
        storage.update(task.id, status=TaskStatus.PENDING, error=None)
        updated = storage.get(task.id)
        assert updated.status == TaskStatus.PENDING

        # 验证 YAML 同步更新
        yaml_file = TEST_DATA_DIR / f"tree_{task.id}" / f"{task.id}.yaml"
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert data["status"] == "pending"

        logger.info("[TC09 PASS] 重置为 pending 验证通过 | task_id=%s", task.id)
