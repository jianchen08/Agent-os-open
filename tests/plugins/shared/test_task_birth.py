# @feature: FP-0.2.〇 任务出生契约统一 | @vision: V2 全能闭环 | @ci: python-coverage
"""task_birth 统一出生协议契约测试（task_submit 与 tasks http_api 共用写面）。

覆盖 ``plugins/shared/task_birth.py`` 三段式协议的行为契约：

- 三段顺序与参数形状：出生登记（create + no_dispatch，出生 state 原样透传）
  → 身份登记（task.id = 引擎管道 id，先于执行）→ 执行派发（kickoff +
  background，execution_context/agent_id/thread_id 按声明透传）；
- 失败即报错（用户裁定：不要有降级路径）：任一阶段 send 异常 / 响应缺
  pipeline_id / 回带 id 与出生 id 不一致 → TaskBirthError（__cause__ 保留），
  且失败阶段之后的阶段零调用（前缀性质）；
- 协议违约即拒绝：出生 state 为空 / user_id 为空 → 未发任何调用即报错。

意图：该协议是任务出生的唯一实现——init 体插件（workspace_lifecycle 工作区
共享决策）依赖 task.id 在任何管道步骤前已在 state，唤醒注入依赖出生登记落的
pipeline_sessions 映射；协议破一处即子任务工作区回归 / 唤醒链断裂。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit  # TDD 分层：纯单测，零外部依赖（tests/plugins 强制）

import task_birth  # noqa: E402  (conftest 已推 plugins/shared 上 sys.path)
from task_birth import TaskBirthError, birth_task_pipeline  # noqa: E402


class _FakeSend:
    """记录 send_message 参数的通道 fake（可编程每段响应/异常）。"""

    def __init__(
        self,
        responses: list[dict] | None = None,
        error_on: set[int] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._responses = responses or []
        self._error_on = error_on or set()

    async def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        if len(self.calls) in self._error_on:
            raise RuntimeError(f"kernel down #{len(self.calls)}")
        if self._responses:
            return self._responses[min(len(self.calls), len(self._responses)) - 1]
        return {"status": "recorded", "pipeline_id": "pipe_engine_gen_1"}


_BIRTH_STATE = {
    "task.goal": "写报告",
    "task.status": "pending",
    "lineage.parent_pipeline_id": "pipe_parent_9",
}


async def _birth(send, **over):
    params = {
        "title": "写报告",
        "birth_state": dict(_BIRTH_STATE),
        "kickoff": "执行任务「写报告」。",
        "user_id": "user-1",
    }
    params.update(over)
    return await birth_task_pipeline(send, **params)


class TestThreePhaseBirthShape:
    async def test_parent_form_three_calls_in_order(self) -> None:
        """有父形式：出生登记 → 身份登记 → 执行派发，三段参数各有其形。"""
        send = _FakeSend()
        pid = await _birth(send)
        assert pid == "pipe_engine_gen_1"
        assert len(send.calls) == 3

        birth, identity, dispatch = send.calls
        # ① 出生登记：create + no_dispatch（只登记不派发），state 原样透传
        assert birth["create"] is True
        assert birth["no_dispatch"] is True
        assert "pipeline_id" not in birth
        assert birth["state"] == _BIRTH_STATE
        assert "写报告" in birth["message"]
        assert birth["user_id"] == "user-1"
        # ② 身份登记：task.id = 出生 id，先于任何管道步骤
        assert identity["pipeline_id"] == "pipe_engine_gen_1"
        assert identity["no_dispatch"] is True
        assert identity["state"] == {"task.id": "pipe_engine_gen_1"}
        # ③ 执行派发：注入分支 + background
        assert dispatch["pipeline_id"] == "pipe_engine_gen_1"
        assert dispatch["message"] == "执行任务「写报告」。"
        assert dispatch["background"] is True
        # 未声明项不注入（execution_context/agent_id/thread_id 缺省不出现）
        for call in send.calls:
            assert "execution_context" not in call
            assert "agent_id" not in call
            assert "thread_id" not in call

    async def test_root_form_and_optional_fields_passthrough(self) -> None:
        """根形式出生 state + 可选字段按声明透传到对应阶段。"""
        send = _FakeSend()
        state = {
            "task.goal": "独立任务",
            "lineage.root": True,
            "lineage.origin.kind": "plugin",
        }
        pid = await _birth(
            send,
            title="独立任务",
            birth_state=state,
            agent_id="code_writer",
            execution_context={"workspace": {"mode": "worktree"}, "isolation": {"level": "isolated"}},
            thread_id="thread-user-1",
        )
        assert pid == "pipe_engine_gen_1"
        birth, _identity, dispatch = send.calls
        assert birth["state"] == state
        assert birth["thread_id"] == "thread-user-1"
        assert birth["agent_id"] == "code_writer"
        # execution_context 是派发参数：只在阶段三出现
        assert "execution_context" not in birth
        assert dispatch["execution_context"]["workspace"]["mode"] == "worktree"
        assert dispatch["agent_id"] == "code_writer"

    @pytest.mark.parametrize(
        ("responses", "pid_after"),
        [
            (None, "pipe_engine_gen_1"),
            ([{"pipeline_id": "abc123def456"}] * 3, "abc123def456"),
        ],
    )
    async def test_returned_pid_equals_birth_response_id(self, responses, pid_after) -> None:
        """性质：返回值恒等于出生登记响应的 pipeline_id（身份权威在引擎 id）。"""
        send = _FakeSend(responses=responses)
        pid = await _birth(send)
        assert pid == pid_after
        assert send.calls[1]["state"]["task.id"] == pid_after


class TestFailLoudNoDegradation:
    async def test_phase1_send_error_raises(self) -> None:
        send = _FakeSend(error_on={1})
        with pytest.raises(TaskBirthError, match="出生登记") as ei:
            await _birth(send)
        assert "kernel down #1" in str(ei.value)
        assert ei.value.__cause__ is not None
        assert len(send.calls) == 1, "出生失败后身份/派发零调用"

    async def test_phase2_send_error_raises_and_never_dispatches(self) -> None:
        """身份登记失败必须报错——身份不全的管道不得启动执行。"""
        send = _FakeSend(error_on={2})
        with pytest.raises(TaskBirthError, match="身份登记"):
            await _birth(send)
        assert len(send.calls) == 2, "身份失败后执行派发零调用"
        assert "background" not in send.calls[-1]

    async def test_phase3_response_missing_pipeline_id_raises(self) -> None:
        send = _FakeSend(responses=[
            {"pipeline_id": "pipe_engine_gen_1"},
            {"pipeline_id": "pipe_engine_gen_1"},
            {"status": "dispatched"},
        ])
        with pytest.raises(TaskBirthError, match="执行派发.*pipeline_id"):
            await _birth(send)
        assert len(send.calls) == 3

    async def test_phase2_id_mismatch_rejected(self) -> None:
        """回带 id 与出生 id 不一致 = 误路由，拒绝继续（不静默跑错管道）。"""
        send = _FakeSend(responses=[
            {"pipeline_id": "pipe_engine_gen_1"},
            {"pipeline_id": "another_pipe_9"},
        ])
        with pytest.raises(TaskBirthError, match="不匹配"):
            await _birth(send)
        assert len(send.calls) == 2

    @pytest.mark.parametrize("field", ["birth_state", "user_id"])
    async def test_protocol_violation_rejected_before_any_call(self, field) -> None:
        """出生 state / user_id 缺失即协议违约：零调用直接报错。"""
        send = _FakeSend()
        over: dict = {"birth_state": dict(_BIRTH_STATE), "user_id": "user-1"}
        over[field] = {} if field == "birth_state" else ""
        with pytest.raises(TaskBirthError):
            await birth_task_pipeline(send, title="T", kickoff="K", **over)
        assert send.calls == []
