# @feature: FP-0.2.〇 任务出生契约统一 | @vision: V2 全能闭环 | @ci: python-coverage
"""task_birth 统一出生协议契约测试（task_submit 与 tasks http_api 共用写面）。

覆盖 ``plugins/shared/task_birth.py`` 四段式协议的行为契约
（ADR-2026-10-17-task-birth-workspace-inclusion：三段扩四段，工作空间创建
纳入提交期同步准备，失败即失败并回传）：

- 四段顺序与参数形状：出生登记（create + no_dispatch，出生 state 原样透传，
  声明 agent_id 时追加 agent.id 身份键）
  → 身份登记（task.id = 引擎管道 id，先于执行）
  → 工作空间创建（同步准备，坐标先落 state 再派发）
  → 执行派发（kickoff + background，execution_context/agent_id/thread_id 按声明透传）；
- 失败即报错（用户裁定：不要有降级路径）：任一阶段 send 异常 / 响应缺
  pipeline_id / 回带 id 与出生 id 不一致 → TaskBirthError（__cause__ 保留），
  且失败阶段之后的阶段零调用（前缀性质）；
- 工作空间准备失败 = 出生失败：**保留任务记录**（task.status=failed +
  task.error=原因）后报错，执行派发零调用；准备返回无 workspace 坐标（幻影
  路径）同判失败；
- 协议违约即拒绝：出生 state 为空 / user_id 为空 / 无准备器 → 未发任何调用
  即报错。

意图：该协议是任务出生的唯一实现——init 体插件（workspace_lifecycle 工作区
共享决策）依赖 task.id 在任何管道步骤前已在 state，唤醒注入依赖出生登记落的
pipeline_sessions 映射；协议破一处即子任务工作区回归 / 唤醒链断裂。工作空间
创建提前到派发之前，使「提交成功」不再与「任务能跑起来」脱钩。
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


class _FakePreparer:
    """工作空间准备器 fake（外部依赖替身：返回坐标 dict 或抛异常）。

    契约：返回工作空间坐标 state 更新（必须含非空 ``workspace``），失败抛异常。
    """

    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result
        self._error = error

    async def __call__(self, *, pipeline_id: str, state: dict, execution_context: dict) -> dict:
        self.calls.append(
            {
                "pipeline_id": pipeline_id,
                "state": dict(state),
                "execution_context": dict(execution_context),
            }
        )
        if self._error is not None:
            raise self._error
        return dict(self._result or {})


_WS_READY = {
    "workspace": "D:/ws/pipe_engine_gen_1",
    "task.ws_meta": {"mode": "worktree", "path": "D:/ws/pipe_engine_gen_1"},
}

_BIRTH_STATE = {
    "task.goal": "写报告",
    "task.status": "pending",
    "lineage.parent_pipeline_id": "pipe_parent_9",
}


async def _birth(send, preparer: _FakePreparer | None = None, **over):
    params = {
        "title": "写报告",
        "birth_state": dict(_BIRTH_STATE),
        "kickoff": "执行任务「写报告」。",
        "user_id": "user-1",
        "prepare_workspace": preparer or _FakePreparer(_WS_READY),
    }
    params.update(over)
    return await birth_task_pipeline(send, **params)


class TestFourPhaseBirthShape:
    async def test_parent_form_four_calls_in_order(self) -> None:
        """有父形式：出生登记 → 身份登记 → 工作空间创建 → 执行派发，四段参数各有其形。"""
        send = _FakeSend()
        pid = await _birth(send)
        assert pid == "pipe_engine_gen_1"
        assert len(send.calls) == 4

        birth, identity, ws_write, dispatch = send.calls
        # ① 出生登记：create + no_dispatch（只登记不派发），state 透传并注入
        # 统一管道标题键（ADR 2026-10-27-pipeline-title-field：title=任务标题）
        assert birth["create"] is True
        assert birth["no_dispatch"] is True
        assert "pipeline_id" not in birth
        assert birth["state"] == {**_BIRTH_STATE, "title": "写报告"}
        assert "写报告" in birth["message"]
        assert birth["user_id"] == "user-1"
        # ② 身份登记：task.id = 出生 id，先于任何管道步骤
        assert identity["pipeline_id"] == "pipe_engine_gen_1"
        assert identity["no_dispatch"] is True
        assert identity["state"] == {"task.id": "pipe_engine_gen_1"}
        # ③ 工作空间坐标落 state（no_dispatch，不派发）——派发前坐标必在位
        assert ws_write["pipeline_id"] == "pipe_engine_gen_1"
        assert ws_write["no_dispatch"] is True
        assert ws_write["state"] == {"task.id": "pipe_engine_gen_1", **_WS_READY}
        # ④ 执行派发：注入分支 + background
        assert dispatch["pipeline_id"] == "pipe_engine_gen_1"
        assert dispatch["message"] == "执行任务「写报告」。"
        assert dispatch["background"] is True
        # 未声明项不注入（execution_context/agent_id/thread_id 缺省不出现）
        for call in send.calls:
            assert "execution_context" not in call
            assert "agent_id" not in call
            assert "thread_id" not in call

    async def test_preparer_receives_task_id_state_and_context(self) -> None:
        """准备器入参 = 身份已登记的坐标：task_id + 含 task.id 的出生 state +
        execution_context（工作空间声明据此解析；目录名需 task_id 故必须后于
        身份登记）。"""
        send = _FakeSend()
        preparer = _FakePreparer(_WS_READY)
        ec = {"workspace": {"source_path": "D:/proj", "mode": "worktree"}}
        await _birth(send, preparer=preparer, execution_context=ec)
        assert len(preparer.calls) == 1
        call = preparer.calls[0]
        assert call["pipeline_id"] == "pipe_engine_gen_1"
        assert call["state"]["task.id"] == "pipe_engine_gen_1"
        assert call["state"]["lineage.parent_pipeline_id"] == "pipe_parent_9"
        assert call["execution_context"] == ec

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
        birth, _identity, _ws_write, dispatch = send.calls
        # agent.id / title 是出生 state 的一部分（身份与命名随快照走），
        # 不是独立派发参数的副作用（title 注入见 ADR 2026-10-27-pipeline-title-field）
        assert birth["state"] == {**state, "agent.id": "code_writer", "title": "独立任务"}
        assert birth["thread_id"] == "thread-user-1"
        assert birth["agent_id"] == "code_writer"
        # execution_context 是派发参数：只在阶段四出现
        assert "execution_context" not in birth
        assert dispatch["execution_context"]["workspace"]["mode"] == "worktree"
        assert dispatch["agent_id"] == "code_writer"

    async def test_agent_id_lands_in_birth_state_snapshot(self) -> None:
        """执行 agent 身份随出生 state 落快照（agent.id）：续跑/冷恢复据此解析，
        不随催促方线程绑定漂移（事故锚 75a097118e75）。未声明时不注入该键。"""
        send = _FakeSend()
        await _birth(send, agent_id="general_agent")
        birth, _identity, _ws_write, dispatch = send.calls
        assert birth["state"]["agent.id"] == "general_agent"
        assert dispatch["agent_id"] == "general_agent"

        send2 = _FakeSend()
        await _birth(send2)
        assert "agent.id" not in send2.calls[0]["state"], "未声明 agent 不得注入身份键"
        assert "agent_id" not in send2.calls[3]

    @pytest.mark.parametrize(
        ("responses", "pid_after"),
        [
            (None, "pipe_engine_gen_1"),
            ([{"pipeline_id": "abc123def456"}] * 4, "abc123def456"),
        ],
    )
    async def test_returned_pid_equals_birth_response_id(self, responses, pid_after) -> None:
        """性质：返回值恒等于出生登记响应的 pipeline_id（身份权威在引擎 id）。"""
        send = _FakeSend(responses=responses)
        pid = await _birth(send)
        assert pid == pid_after
        assert send.calls[1]["state"]["task.id"] == pid_after


class TestWorkspacePrepareFailure:
    """工作空间准备失败 = 出生失败：任务记录保留 + 标 failed + 派发零调用。

    ADR-2026-10-17-task-birth-workspace-inclusion 的核心契约——提交成功不得与
    「工作空间能建起来」脱钩；不留记录则磁盘留下无任务归属的孤儿 worktree。
    """

    async def test_prepare_error_marks_task_failed_and_blocks_dispatch(self) -> None:
        send = _FakeSend()
        preparer = _FakePreparer(error=RuntimeError("git worktree add 失败: 跨盘"))
        with pytest.raises(TaskBirthError, match="工作空间创建") as ei:
            await _birth(send, preparer=preparer)
        assert "git worktree add 失败: 跨盘" in str(ei.value)
        assert ei.value.__cause__ is not None
        # 出生登记 + 身份登记 + 失败随写 = 3 次 send；执行派发零调用
        assert len(send.calls) == 3
        assert all("background" not in call for call in send.calls)
        marker = send.calls[-1]
        assert marker["no_dispatch"] is True
        assert marker["state"]["task.status"] == "failed"
        assert "git worktree add 失败: 跨盘" in marker["state"]["task.error"]
        assert marker["state"]["task.id"] == "pipe_engine_gen_1"

    @pytest.mark.parametrize(
        ("result", "label"),
        [
            ({}, "空返回"),
            ({"workspace": ""}, "空坐标"),
            ({"task.ws_meta": {"mode": "plain"}}, "缺 workspace 键"),
            ({"workspace": 123}, "非字符串坐标"),
        ],
    )
    async def test_result_without_real_workspace_is_failure(self, result, label) -> None:
        """性质：准备器未交出真实工作空间坐标 = 未就绪 = 失败（禁幻影路径裸奔）。

        与「真就绪」输入构成有区分度两组（见 test_ready_workspace_dispatches）。
        """
        send = _FakeSend()
        preparer = _FakePreparer(result)
        with pytest.raises(TaskBirthError, match="工作空间创建"):
            await _birth(send, preparer=preparer)
        assert len(send.calls) == 3, f"{label}：失败随写后不得派发"
        assert send.calls[-1]["state"]["task.status"] == "failed"
        assert send.calls[-1]["state"]["task.error"]

    async def test_ready_workspace_dispatches(self) -> None:
        """对照：交出真实坐标 → 坐标先落 state 再派发（正常路径不误判失败）。"""
        send = _FakeSend()
        pid = await _birth(send, preparer=_FakePreparer(_WS_READY))
        assert pid == "pipe_engine_gen_1"
        assert send.calls[2]["state"]["workspace"] == _WS_READY["workspace"]
        assert send.calls[3]["background"] is True
        assert all("task.error" not in call.get("state", {}) for call in send.calls)

    async def test_dispatch_error_marks_task_failed_keeps_workspace(self) -> None:
        """阶段四派发调用本身抛错 → 任务不会跑，写失败终态（免得面板悬一条
        永不推进的 pending）；工作空间保留不删（任务记录即清理锚点）。"""
        send = _FakeSend(error_on={4})
        preparer = _FakePreparer(_WS_READY)
        with pytest.raises(TaskBirthError, match="执行派发"):
            await _birth(send, preparer=preparer)
        assert len(send.calls) == 5, "派发失败随写一次"
        marker = send.calls[-1]
        assert marker["state"]["task.status"] == "failed"
        assert "执行派发失败" in marker["state"]["task.error"]
        # 工作空间坐标仍在 state 里（未回滚、未删除）
        assert send.calls[2]["state"]["workspace"] == _WS_READY["workspace"]

    async def test_dispatch_response_shape_error_does_not_mark_failed(self) -> None:
        """派发响应形态异常（缺 pipeline_id）→ 内核 spawn 已发生、任务真在跑，
        改写状态即谎报：零写直接上报。"""
        send = _FakeSend(responses=[
            {"pipeline_id": "pipe_engine_gen_1"},
            {"pipeline_id": "pipe_engine_gen_1"},
            {"pipeline_id": "pipe_engine_gen_1"},
            {"status": "dispatched"},
        ])
        with pytest.raises(TaskBirthError, match="执行派发.*pipeline_id"):
            await _birth(send)
        assert len(send.calls) == 4, "不得追加失败随写"
        assert all(
            (c.get("state") or {}).get("task.status") != "failed" for c in send.calls
        )

    async def test_failure_marker_write_error_does_not_mask_prepare_failure(self) -> None:
        """失败随写本身失败（内核不可达）也不得改判：准备失败仍是出生失败，
        原始原因保留在异常消息里（错误是值，随写失败只增留痕）。"""
        send = _FakeSend(error_on={3})
        preparer = _FakePreparer(error=RuntimeError("worktree 未落地"))
        with pytest.raises(TaskBirthError, match="工作空间创建") as ei:
            await _birth(send, preparer=preparer)
        assert "worktree 未落地" in str(ei.value)
        assert len(send.calls) == 3


class TestFailLoudNoDegradation:
    async def test_phase1_send_error_raises(self) -> None:
        send = _FakeSend(error_on={1})
        with pytest.raises(TaskBirthError, match="出生登记") as ei:
            await _birth(send)
        assert "kernel down #1" in str(ei.value)
        assert ei.value.__cause__ is not None
        assert len(send.calls) == 1, "出生失败后身份/派发零调用"

    async def test_phase2_send_error_raises_and_never_prepares(self) -> None:
        """身份登记失败必须报错——身份不全的管道不得准备工作空间、不得执行。"""
        send = _FakeSend(error_on={2})
        preparer = _FakePreparer(_WS_READY)
        with pytest.raises(TaskBirthError, match="身份登记"):
            await _birth(send, preparer=preparer)
        assert preparer.calls == [], "身份失败后工作空间创建零调用"

    async def test_phase4_response_missing_pipeline_id_raises(self) -> None:
        send = _FakeSend(responses=[
            {"pipeline_id": "pipe_engine_gen_1"},
            {"pipeline_id": "pipe_engine_gen_1"},
            {"pipeline_id": "pipe_engine_gen_1"},
            {"status": "dispatched"},
        ])
        with pytest.raises(TaskBirthError, match="执行派发.*pipeline_id"):
            await _birth(send)
        assert len(send.calls) == 4

    async def test_phase2_id_mismatch_rejected(self) -> None:
        """回带 id 与出生 id 不一致 = 误路由，拒绝继续（不静默跑错管道）。"""
        send = _FakeSend(responses=[
            {"pipeline_id": "pipe_engine_gen_1"},
            {"pipeline_id": "another_pipe_9"},
        ])
        with pytest.raises(TaskBirthError, match="不匹配"):
            await _birth(send)
        assert len(send.calls) == 2

    @pytest.mark.parametrize("field", ["birth_state", "user_id", "prepare_workspace"])
    async def test_protocol_violation_rejected_before_any_call(self, field) -> None:
        """出生 state / user_id / 准备器缺失即协议违约：零调用直接报错。"""
        send = _FakeSend()
        over: dict = {
            "birth_state": dict(_BIRTH_STATE),
            "user_id": "user-1",
            "prepare_workspace": _FakePreparer(_WS_READY),
        }
        over[field] = {} if field == "birth_state" else ("" if field == "user_id" else None)
        with pytest.raises(TaskBirthError):
            await birth_task_pipeline(send, title="T", kickoff="K", **over)
        assert send.calls == []


class TestInheritedCoordinate:
    """继承坐标：工作空间已就位 → 阶段三无创建动作（2026-10-20 裁定）。

    2026-10-20 用户裁定《继承机制简化方案》：接续任务落在源任务的同一
    worktree/分支（坐标整份复制），工作空间**不新建**——出生 state 带
    ``task.inherit_from`` + 整份复制的 ``task.ws_meta`` 即为坐标已就位的
    声明，准备器走幂等跳过分支返回空坐标更新属预期。
    """

    _INHERIT_META = {
        "mode": "worktree",
        "path": "D:/ws/wt_src",
        "project_root": "D:/proj",
        "branch": "task/src000000001",
        "tree_path": "devA/wt_src",
    }

    def _inherit_state(self) -> dict:
        return {
            **dict(_BIRTH_STATE),
            "task.inherit_from": "src000000001",
            "task.ws_meta": dict(self._INHERIT_META),
            "workspace": self._INHERIT_META["path"],
        }

    async def test_preparer_skip_uses_inherited_coordinate(self) -> None:
        """准备器幂等跳过（空坐标更新）→ 以继承坐标为准并登记。"""
        send = _FakeSend()
        pid = await _birth(send, preparer=_FakePreparer({}), birth_state=self._inherit_state())
        assert pid == "pipe_engine_gen_1"
        ws_write = send.calls[2]
        assert ws_write["state"]["workspace"] == self._INHERIT_META["path"]
        assert ws_write["state"]["task.ws_meta"] == self._INHERIT_META

    async def test_preparer_same_path_keeps_inherited_coordinate(self) -> None:
        """准备器交出同一落点 → 坐标仍取继承副本（分支/树路径不被稀释）。"""
        send = _FakeSend()
        state = self._inherit_state()
        preparer = _FakePreparer(
            {
                "workspace": state["workspace"],
                "task.ws_meta": {"mode": "worktree", "path": state["workspace"]},
            }
        )
        await _birth(send, preparer=preparer, birth_state=state)
        ws_write = send.calls[2]
        assert ws_write["state"]["task.ws_meta"] == self._INHERIT_META

    async def test_preparer_drifted_path_fails_loud(self) -> None:
        """准备器交出与继承坐标不一致的落点 → 出生失败（坐标漂移不静默放行）。"""
        send = _FakeSend()
        preparer = _FakePreparer(
            {
                "workspace": "D:/ws/other",
                "task.ws_meta": {"mode": "worktree", "path": "D:/ws/other"},
            }
        )
        with pytest.raises(TaskBirthError, match="不一致"):
            await _birth(send, preparer=preparer, birth_state=self._inherit_state())
        # 执行派发零调用（失败随写保留任务记录）
        assert all("background" not in call for call in send.calls)
        marker = send.calls[-1]
        assert marker["state"]["task.status"] == "failed"
