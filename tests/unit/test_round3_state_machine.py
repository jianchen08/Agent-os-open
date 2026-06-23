"""
Round 3 — 任务状态机完整转换矩阵测试。
聚焦：7x7 状态转换矩阵（49 单元）的明确覆盖。
对应 AC：AC-TASK-01
"""
from __future__ import annotations
import pytest
from src.tasks.state_machine import (
    InvalidTransitionError,
    get_task_state_machine,
    _TASK_TRANSITIONS,
)

ALL_STATES = ["pending", "running", "evaluating", "stopped", "completed", "failed", "timeout"]


class TestMatrixIntegrity:
    def test_all_7_statuses_present(self):
        expected = {"pending", "running", "evaluating", "stopped", "completed", "failed", "timeout"}
        assert set(_TASK_TRANSITIONS.keys()) == expected

    def test_pending(self):
        assert set(_TASK_TRANSITIONS["pending"]) == {"running", "stopped", "completed", "failed"}

    def test_running(self):
        assert set(_TASK_TRANSITIONS["running"]) == {"evaluating", "completed", "failed", "stopped", "timeout"}

    def test_evaluating(self):
        assert set(_TASK_TRANSITIONS["evaluating"]) == {"running", "completed", "failed", "stopped"}

    def test_stopped(self):
        assert set(_TASK_TRANSITIONS["stopped"]) == {"running", "pending"}

    def test_completed(self):
        assert set(_TASK_TRANSITIONS["completed"]) == {"pending"}

    def test_failed(self):
        assert set(_TASK_TRANSITIONS["failed"]) == {"pending", "running"}

    def test_timeout(self):
        assert set(_TASK_TRANSITIONS["timeout"]) == {"running", "pending", "failed"}

    def test_total_legal_is_21(self):
        assert sum(len(v) for v in _TASK_TRANSITIONS.values()) == 21


_LEGAL = [(f, t) for f, ts in _TASK_TRANSITIONS.items() for t in ts]


class TestLegalTransitions:
    @pytest.mark.parametrize("from_s, to_s", _LEGAL, ids=[f"{f}->{t}" for f, t in _LEGAL])
    def test_can(self, from_s, to_s):
        sm = get_task_state_machine()
        sm._current_state = from_s
        assert sm.can_transition(to_s) is True

    @pytest.mark.parametrize("from_s, to_s", _LEGAL, ids=[f"{f}->{t}" for f, t in _LEGAL])
    def test_executes(self, from_s, to_s):
        sm = get_task_state_machine()
        sm._current_state = from_s
        sm.transition(to_s)
        assert sm.current_state == to_s


_ILLEGAL = [(f, t) for f in ALL_STATES for t in ALL_STATES if f != t and t not in _TASK_TRANSITIONS.get(f, [])]


class TestIllegalTransitions:
    def test_count_is_21(self):
        assert len(_ILLEGAL) == 21

    @pytest.mark.parametrize("from_s, to_s", _ILLEGAL, ids=[f"{f}->{t}" for f, t in _ILLEGAL])
    def test_cannot(self, from_s, to_s):
        sm = get_task_state_machine()
        sm._current_state = from_s
        assert sm.can_transition(to_s) is False

    @pytest.mark.parametrize("from_s, to_s", _ILLEGAL, ids=[f"{f}->{t}" for f, t in _ILLEGAL])
    def test_raises(self, from_s, to_s):
        sm = get_task_state_machine()
        sm._current_state = from_s
        with pytest.raises(InvalidTransitionError) as exc:
            sm.transition(to_s)
        assert exc.value.current_state == from_s
        assert exc.value.target_state == to_s


class TestSelfTransitions:
    @pytest.mark.parametrize("state", ALL_STATES)
    def test_self_cannot(self, state):
        sm = get_task_state_machine()
        sm._current_state = state
        assert sm.can_transition(state) is False

    @pytest.mark.parametrize("state", ALL_STATES)
    def test_self_raises(self, state):
        sm = get_task_state_machine()
        sm._current_state = state
        with pytest.raises(InvalidTransitionError):
            sm.transition(state)


class TestTransitionChains:
    def test_normal_lifecycle(self):
        sm = get_task_state_machine()
        sm.transition("running"); sm.transition("evaluating"); sm.transition("completed")
        assert sm.current_state == "completed"

    def test_failed_then_retry(self):
        sm = get_task_state_machine()
        sm.transition("running"); sm.transition("failed"); sm.transition("running"); sm.transition("completed")
        assert sm.current_state == "completed"

    def test_stop_and_resume(self):
        sm = get_task_state_machine()
        sm.transition("running"); sm.transition("stopped"); sm.transition("running")
        assert sm.current_state == "running"

    def test_timeout_recovery(self):
        sm = get_task_state_machine()
        sm.transition("running"); sm.transition("timeout"); sm.transition("running"); sm.transition("completed")
        assert sm.current_state == "completed"

    def test_timeout_to_failed(self):
        sm = get_task_state_machine()
        sm.transition("running"); sm.transition("timeout"); sm.transition("failed")
        assert sm.current_state == "failed"

    def test_completed_requeue(self):
        sm = get_task_state_machine()
        sm.transition("running"); sm.transition("completed"); sm.transition("pending"); sm.transition("running")
        assert sm.current_state == "running"


class TestMatrixCoverage:
    def test_49_cells(self):
        legal, illegal, self_t = 0, 0, 0
        for f in ALL_STATES:
            for t in ALL_STATES:
                if f == t: self_t += 1
                elif t in _TASK_TRANSITIONS.get(f, []): legal += 1
                else: illegal += 1
        assert legal == 21 and illegal == 21 and self_t == 7
        assert legal + illegal + self_t == 49

    def test_factory_default_pending(self):
        assert get_task_state_machine().current_state == "pending"
