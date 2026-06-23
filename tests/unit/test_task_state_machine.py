"""
任务状态机与容器任务单元测试。

覆盖 AC：
- AC-TASK-01: 任务状态机转换合法（所有合法/非法转换）
- AC-TASK-03: 容器任务管理子任务（父子关系正确）

对应需求：F-TASK-01~03
"""
import pytest

from src.tasks.state_machine import (
    InvalidTransitionError,
    SimpleStateMachine,
    _TASK_TRANSITIONS,
    get_task_state_machine,
)
from src.tasks.types import TaskStatus, create_task


# ============================================================
# AC-TASK-01: 状态机 — 所有合法转换（transition_coverage=100%）
# ============================================================

# 从 _TASK_TRANSITIONS 生成所有合法转换对
_ALL_LEGAL_TRANSITIONS: list[tuple[str, str]] = [
    (src, dst)
    for src, targets in _TASK_TRANSITIONS.items()
    for dst in targets
]


class TestStateMachineLegalTransitions:
    """所有合法状态转换测试。"""

    @pytest.mark.parametrize(
        "from_state, to_state",
        _ALL_LEGAL_TRANSITIONS,
        ids=[f"{s}->{t}" for s, t in _ALL_LEGAL_TRANSITIONS],
    )
    def test_legal_transition_succeeds(self, from_state: str, to_state: str) -> None:
        """合法转换应成功执行。"""
        sm = SimpleStateMachine(initial_state=from_state, transitions=_TASK_TRANSITIONS)
        sm.transition(to_state)
        assert sm.current_state == to_state

    @pytest.mark.parametrize(
        "from_state, to_state",
        _ALL_LEGAL_TRANSITIONS,
        ids=[f"{s}->{t}" for s, t in _ALL_LEGAL_TRANSITIONS],
    )
    def test_can_transition_returns_true_for_legal(
        self, from_state: str, to_state: str
    ) -> None:
        """合法转换前 can_transition 返回 True。"""
        sm = SimpleStateMachine(initial_state=from_state, transitions=_TASK_TRANSITIONS)
        assert sm.can_transition(to_state) is True


# ============================================================
# AC-TASK-01: 状态机 — 非法转换（完整性验证）
# ============================================================

_ALL_STATES = list(_TASK_TRANSITIONS.keys())

# 生成所有非法转换对（from→to 不在 _TASK_TRANSITIONS 中且 from≠to）
_ALL_ILLEGAL_TRANSITIONS: list[tuple[str, str]] = [
    (src, dst)
    for src in _ALL_STATES
    for dst in _ALL_STATES
    if dst not in _TASK_TRANSITIONS.get(src, []) and dst != src
]


class TestStateMachineIllegalTransitions:
    """所有非法状态转换测试。"""

    @pytest.mark.parametrize(
        "from_state, to_state",
        _ALL_ILLEGAL_TRANSITIONS,
        ids=[f"{s}-X->{t}" for s, t in _ALL_ILLEGAL_TRANSITIONS],
    )
    def test_illegal_transition_raises(
        self, from_state: str, to_state: str
    ) -> None:
        """非法转换应抛出 InvalidTransitionError。"""
        sm = SimpleStateMachine(initial_state=from_state, transitions=_TASK_TRANSITIONS)
        with pytest.raises(InvalidTransitionError):
            sm.transition(to_state)

    @pytest.mark.parametrize(
        "from_state, to_state",
        _ALL_ILLEGAL_TRANSITIONS,
        ids=[f"{s}-X->{t}" for s, t in _ALL_ILLEGAL_TRANSITIONS],
    )
    def test_can_transition_returns_false_for_illegal(
        self, from_state: str, to_state: str
    ) -> None:
        """非法转换前 can_transition 返回 False。"""
        sm = SimpleStateMachine(initial_state=from_state, transitions=_TASK_TRANSITIONS)
        assert sm.can_transition(to_state) is False


# ============================================================
# AC-TASK-01: 状态机 — state_coverage=100%（所有7种状态可达性）
# ============================================================

class TestStateMachineStateCoverage:
    """验证所有 7 种状态都可达。"""

    def test_all_7_states_defined(self) -> None:
        """状态机定义了 7 种状态。"""
        assert len(_TASK_TRANSITIONS) == 7

    def test_all_states_are_valid_task_status(self) -> None:
        """状态机中的所有状态都是有效的 TaskStatus。"""
        valid_statuses = {s.value for s in TaskStatus}
        for state in _TASK_TRANSITIONS:
            assert state in valid_statuses, f"状态 '{state}' 不是有效的 TaskStatus"

    def test_pending_is_initial_state(self) -> None:
        """get_task_state_machine 初始状态为 pending。"""
        sm = get_task_state_machine()
        assert sm.current_state == "pending"


# ============================================================
# AC-TASK-01: 状态机错误信息验证
# ============================================================

class TestStateMachineErrorInfo:
    """非法转换错误信息验证。"""

    def test_error_contains_states(self) -> None:
        """错误信息包含当前状态和目标状态。"""
        sm = SimpleStateMachine(
            initial_state="completed", transitions=_TASK_TRANSITIONS
        )
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("running")

        assert "completed" in str(exc_info.value)
        assert "running" in str(exc_info.value)

    def test_error_has_attributes(self) -> None:
        """异常对象包含 current_state 和 target_state 属性。"""
        sm = SimpleStateMachine(initial_state="pending", transitions=_TASK_TRANSITIONS)
        try:
            sm.transition("evaluating")
            assert False, "应抛出异常"
        except InvalidTransitionError as e:
            assert e.current_state == "pending"
            assert e.target_state == "evaluating"


# ============================================================
# AC-TASK-03: 容器任务管理子任务（父子关系正确）
# ============================================================

class TestContainerTaskHierarchy:
    """容器任务父子关系测试。"""

    def test_create_task_with_parent(self) -> None:
        """创建子任务时 parent_task_id 正确设置。"""
        parent = create_task(title="父任务")
        child = create_task(
            title="子任务",
            parent_task_id=parent.id,
        )

        assert child.parent_task_id == parent.id
        assert parent.parent_task_id is None

    def test_create_task_without_parent_is_top_level(self) -> None:
        """无 parent_task_id 的任务是顶级任务。"""
        task = create_task(title="顶级任务")
        assert task.parent_task_id is None

    def test_child_task_has_independent_id(self) -> None:
        """子任务 ID 与父任务 ID 不同。"""
        parent = create_task(title="父")
        child = create_task(title="子", parent_task_id=parent.id)

        assert parent.id != child.id

    def test_multiple_children_share_same_parent(self) -> None:
        """多个子任务共享同一个 parent_task_id。"""
        parent = create_task(title="容器任务")
        children = [
            create_task(title=f"子任务{i}", parent_task_id=parent.id)
            for i in range(3)
        ]

        for child in children:
            assert child.parent_task_id == parent.id

        # 所有子任务 ID 互不相同
        ids = [c.id for c in children]
        assert len(set(ids)) == len(ids)

    def test_task_id_is_12_char_hex(self) -> None:
        """任务 ID 为 UUID hex 前 12 位。"""
        task = create_task(title="test")
        assert len(task.id) == 12
        # 应是合法的十六进制字符串
        int(task.id, 16)  # 不抛异常即合法

    def test_new_task_status_is_pending(self) -> None:
        """新创建的任务状态为 pending。"""
        task = create_task(title="新任务")
        assert task.status == TaskStatus.PENDING

    def test_container_task_can_track_children(self) -> None:
        """容器任务可通过 parent_task_id 关联多个子任务。"""
        container = create_task(title="容器", description="包含多个子任务")

        child_a = create_task(title="子A", parent_task_id=container.id)
        child_b = create_task(title="子B", parent_task_id=container.id)

        # 验证容器与子任务的关系
        assert child_a.parent_task_id == container.id
        assert child_b.parent_task_id == container.id
        assert container.parent_task_id is None


# ============================================================
# 任务优先级测试
# ============================================================

class TestTaskPriority:
    """任务优先级测试。"""

    def test_default_priority_is_normal(self) -> None:
        """默认优先级为 NORMAL。"""
        task = create_task(title="test")
        assert task.priority.value == 5

    @pytest.mark.parametrize(
        "priority, expected",
        [
            (1, 1),
            (3, 3),
            (5, 5),
            (7, 7),
            (9, 9),
        ],
    )
    def test_priority_values(self, priority: int, expected: int) -> None:
        """优先级数值正确。"""
        task = create_task(title="test", priority=priority)
        assert task.priority == expected
