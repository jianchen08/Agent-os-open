# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""task_reminder 插件剩余分支补测（缺行清零批）。

锁定以下行为契约（对应 plugin.py / server.py 缺行）：

1. **空回复耗尽的完成证据复查**（599）：``llm_empty_exhausted`` 且任务身份在场时，
   ``evaluation.detected_result`` 是完成证据——终态不被覆盖为 failed，仍收束 end
   （与提醒耗尽同一复查面，已完成态不可覆盖）。
2. **完成证据全集**（497）：``_has_completion_evidence`` = state 完成证据 ∪
   evaluation.detected_result；state 侧命中即 True（短路，不再看 detected_result）。
3. **次级证据的结构化判定**（663/674/689）——``_has_successful_task_evaluate``
   只认「评估通过」（task_evaluate 且 ``metadata.result == "completed"``）的
   证据：信封形态（tool_core 正路，tool_result 字段自名工具）与 content
   JSON 形态（旧序列化/回放兼容，调用 id 配对 + success 语义两形态）；
   - 未配对的 role=tool 消息跳过（不构成证据，哪怕 ID 相近）；
   - JSON 解析成非 dict（列表/标量）跳过（无 success 语义）；
   - metadata 非 completed 裁决（retry/partial_pass/failed/缺席）不构成
     通过证据——未通过误判为通过会把未完成任务提前放行，漏判只回退提醒
     级联（方向安全）；
   - slim 成功形态（无 success 键且无 error）+ metadata completed 视为
     通过——SDK toLLM 面默认序列化。
4. **回退检测只看最后一条有文本的 assistant**（696-705）：非 dict/非 assistant
   消息、空文本 assistant 均跳过；最后一条有文本的 assistant 无 JSON 时返回
   None（更早轮次的 JSON 不作本轮证据）——与「命中即返回」构成对照组。
5. **JSON 候选解析容错**（729-730）：花括号配对候选非法（脏 JSON）时跳过该候选，
   不整体放弃——非法候选之后的合法候选仍可命中（回退链继续）。
6. **活跃子任务的服务回退正路**（777）：state 无 submitted_task_ids 标记时经
   ``tasks.service_access.get_task_service`` 懒加载单例查询——服务可用时以其
   结果为准（无活跃子任务 → 正常催评估）。
7. **server 适配层 dict 直通**（server.py 73）：插件返回 core 型 dict 时原样
   回传（Input/Output 的 PluginResult 展开是另一形态，两形态都要能过）。
8. **失败写点复查面的 DB 权威终态**（2026-10-19 幽灵续跑改判事故修复③）：
   ``_reminder_exhausted_result`` / ``_empty_exhausted_result`` 的复查条件扩为
   ``_has_completion_evidence(state) or db_completed``；``_db_task_status`` 经
   state 聚合读取器取本管道聚合行（task_id==pipeline_id）的 task.status，
   reader 未注入/行缺失/行无 task.status/读面异常 → None（诚实降级）。

**经 execute 不可达的行（保留，直接调用覆盖）**：
- ``_has_completion_evidence`` 的 ``return True``（497）：该复查面的两个调用点
  （``_reminder_exhausted_result`` / ``_empty_exhausted_result``）都在
  ``_execute_inner`` 信号②（``if self._task_completed_in_state(state)``）之后，
  而信号②命中即当轮收束返回、根本不进入耗尽裁决。同一次 execute 内 state 不被
  改写（``_apply_runtime_config`` 只改插件实例属性），故到达复查面时
  ``_task_completed_in_state`` 必为 False，state 侧分支不可达；
  ``evaluation.detected_result`` 侧（498）才是耗尽路径的实际命中面。
  按契约以直接调用覆盖。

[来源: coverage.xml 2026-09-14 缺行清单]
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types as _types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/
_TASKS_DIR = _SHARED / "tasks"  # 裸名 task_types（helper 回退枚举）解析前提（0.2 平铺位）

for _d in (_DIR, _SHARED, _TASKS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module(name: str, filename: str) -> Any:
    """按唯一模块名加载插件文件（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_module("task_reminder_gaps_test", "plugin.py")
TaskReminder = _mod.TaskReminder

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state, config={}, _services=services or {})


def _base(**over: Any) -> dict[str, Any]:
    """0.2 任务管道基线（llm_call 轮 + task.id + 纯文本轮 + L2 叶子执行者）。"""
    state: dict[str, Any] = {
        "core_type": "llm_call",
        "iteration": 4,
        "task.id": "task-gaps",
        "task.status": "running",
        "agent_level": "L2",
        "raw_tool_calls": [],
        "raw_result": "阶段性文本输出",
        "messages": [],
    }
    state.update(over)
    return state


def _run(plugin: Any, state: dict[str, Any], services: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(plugin.execute(_ctx(state, services))).state_updates


def _eval_messages(content: str, call_id: str = "c1") -> list[dict[str, Any]]:
    """task_evaluate 调用 + 对应 tool 结果消息对。"""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": "task_evaluate"}}],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


# 标准评估结论 JSON（评估模式回退检测用）
_EVAL_JSON = '结论：{"evaluation_result": {"passed": true, "score": 88}}'


class _FakeTaskService:
    """最小 task_service 替身：list_subtasks 记录查询并返回预设结果。"""

    def __init__(self, subtasks: list[Any]) -> None:
        self.subtasks = subtasks
        self.queried: list[str] = []

    def list_subtasks(self, task_id: str) -> list[Any]:
        self.queried.append(task_id)
        return self.subtasks


# ═══════════════════════════════════════════════════════════
# 1. 空回复耗尽的完成证据复查
# ═══════════════════════════════════════════════════════════


class TestEmptyExhaustedEvidence:
    """空回复重试耗尽：有完成证据保持现状，无证据才落 failed。"""

    def _empty_state(self, **over: Any) -> dict[str, Any]:
        return _base(raw_result="", llm_empty_exhausted=True, **over)

    def test_detected_result_preserves_status(self) -> None:
        """evaluation.detected_result 在场 → 不写 failed，终态保持、仍收束 end。"""
        state = self._empty_state(**{"evaluation.detected_result": {"passed": True}})
        updates = _run(TaskReminder(), state)
        assert "task.status" not in updates
        assert "task.ended_at" not in updates
        assert updates["ended"] is True
        assert updates["_has_new_llm_input"] is False
        assert state["task.status"] == "running"

    def test_no_evidence_marks_failed(self) -> None:
        """对照：无任何完成证据 → task.status 落 failed（空响应 = 未完成执行义务）。"""
        updates = _run(TaskReminder(), self._empty_state())
        assert updates["task.status"] == "failed"
        assert updates["ended"] is True

    def test_completed_state_evidence_wins_before_exhaustion_branch(self) -> None:
        """对照：task_evaluation_completed 属信号②，在耗尽裁决之前当轮收束——

        终态补落 completed（而非 failed），空响应耗尽的失败分支不参与。
        """
        updates = _run(TaskReminder(), self._empty_state(task_evaluation_completed=True))
        assert updates["task.status"] == "completed"
        assert updates["ended"] is True

    def test_session_pipeline_without_task_id_still_closes(self) -> None:
        """会话管道（无 task.id）：只收束不落任务终态（无任务可失败）。"""
        state = _base(raw_result="", llm_empty_exhausted=True)
        del state["task.id"]
        updates = _run(TaskReminder(), state)
        assert "task.status" not in updates
        assert updates["ended"] is True


class TestCompletionEvidenceUnion:
    """完成证据全集：state 侧与 detected_result 侧任一在场即完成。"""

    @pytest.mark.parametrize(
        "state",
        [
            {"task.status": "completed"},
            {"task_evaluation_completed": True},
            {"evaluation.detected_result": {"passed": False}},
        ],
    )
    def test_any_source_counts(self, state: dict[str, Any]) -> None:
        assert TaskReminder._has_completion_evidence(state) is True

    @pytest.mark.parametrize(
        "state",
        [
            {},
            {"task.status": "running"},
            {"task_evaluation_completed": False},
            {"evaluation.detected_result": None},
        ],
    )
    def test_absent_sources_are_not_evidence(self, state: dict[str, Any]) -> None:
        assert TaskReminder._has_completion_evidence(state) is False

    def test_state_side_short_circuits_detected_result(self) -> None:
        """state 侧命中即真（短路返回，与 detected_result 取值无关）。"""
        assert (
            TaskReminder._has_completion_evidence(
                {"task.status": "completed", "evaluation.detected_result": None}
            )
            is True
        )


# ═══════════════════════════════════════════════════════════
# 2. 次级证据的结构化判定
# ═══════════════════════════════════════════════════════════


class TestSecondaryEvidenceShape:
    """messages 评估证据的精确判定：只有「评估通过」才构成证据。

    两种形态：信封（tool_core 正路，tool_result 字段自名工具）与 content
    JSON（旧序列化/回放兼容，id 配对 + success 两形态）。非 completed 裁决
    （retry/partial_pass/failed/metadata 缺席）一律不是证据——落回提醒级联。
    """

    def _allow_end(self, messages: list[Any]) -> dict[str, Any]:
        """次级证据命中时的可观察结果：当轮收束补落终态（ended=True）。"""
        return _run(TaskReminder(), _base(messages=messages))

    def _envelope_msg(
        self,
        *,
        tool_name: str = "task_evaluate",
        success: bool = True,
        metadata: dict[str, Any] | None = None,
        call_id: str = "c1",
    ) -> dict[str, Any]:
        """tool_core tool_result_message 信封形态（content=YAML data，不参与判定）。"""
        envelope: dict[str, Any] = {
            "call_id": call_id,
            "tool_name": tool_name,
            "success": success,
            "error": None,
            "data": {"overall_passed": True},
        }
        if metadata is not None:
            envelope["metadata"] = metadata
        return {"role": "tool", "tool_call_id": call_id, "content": "metrics: []\n", "tool_result": envelope}

    @pytest.mark.parametrize(
        "content",
        [
            '{"success": true, "metadata": {"result": "completed", "action": "auto_complete"}}',
            '{"output": {"overall_passed": true}, "metadata": {"result": "completed"}}',
        ],
        ids=["full-form", "slim-form"],
    )
    def test_completed_evidence_closes_round(self, content: str) -> None:
        """content JSON 通过证据（全量/slim 形态 + result=completed）→ 收束补落。"""
        updates = self._allow_end(_eval_messages(content))
        assert updates["ended"] is True
        assert updates["task.status"] == "completed"
        assert "evaluate_reminder_count" not in updates

    def test_envelope_completed_evidence_closes_round(self) -> None:
        """信封形态（tool_core 正路）：success + result=completed → 收束补落。"""
        msg = self._envelope_msg(metadata={"result": "completed", "action": "auto_complete"})
        updates = self._allow_end([msg])
        assert updates["ended"] is True
        assert updates["task.status"] == "completed"
        assert "evaluate_reminder_count" not in updates

    def test_envelope_without_caller_pairing_still_evidence(self) -> None:
        """信封自名工具：messages 缺 assistant tool_calls 配对仍构成证据。"""
        msg = self._envelope_msg(metadata={"result": "completed"})
        messages = [{"role": "user", "content": "评估一下"}] + [msg]
        updates = self._allow_end(messages)
        assert updates["ended"] is True

    @pytest.mark.parametrize(
        "metadata",
        [
            {"result": "retry", "retry_remaining": 2},
            {"result": "partial_pass"},
            {"result": "failed"},
            {},
        ],
        ids=["retry", "partial_pass", "failed", "missing-result"],
    )
    def test_non_completed_envelope_not_evidence_reminds(self, metadata: dict[str, Any]) -> None:
        """信封非 completed 裁决（含 metadata 缺席）→ 不是通过证据，提醒照常注入。"""
        msg = self._envelope_msg(metadata=metadata)
        updates = self._allow_end([msg])
        assert updates.get("evaluate_reminder_count") == 1
        assert "ended" not in updates

    def test_envelope_failed_tool_not_evidence(self) -> None:
        """工具执行失败（success=False）的信封不构成证据（评估未发生）。"""
        msg = self._envelope_msg(success=False, metadata={"result": "completed"})
        updates = self._allow_end([msg])
        assert updates.get("evaluate_reminder_count") == 1

    def test_envelope_other_tool_not_evidence(self) -> None:
        """其他工具的信封（name 非 task_evaluate）不构成证据。"""
        msg = self._envelope_msg(tool_name="bash_execute", metadata={"result": "completed"})
        updates = self._allow_end([msg])
        assert updates.get("evaluate_reminder_count") == 1

    def test_unpaired_tool_message_skipped_then_paired_hit(self) -> None:
        """未配对的 role=tool 消息被跳过；配对的 slim 通过证据仍命中。"""
        messages = [
            {"role": "tool", "tool_call_id": "unrelated-id", "content": '{"success": true}'},
            {"role": "assistant", "content": "无工具调用"},
        ] + _eval_messages('{"output": "done", "metadata": {"result": "completed"}}')
        updates = self._allow_end(messages)
        assert updates["ended"] is True

    @pytest.mark.parametrize(
        "content",
        [
            "[1, 2, 3]",  # JSON 合法但非 dict
            '"just a string"',
            "42",
        ],
    )
    def test_non_dict_json_payload_not_evidence(self, content: str) -> None:
        """非 dict 载荷无 success 语义 → 不构成证据（提醒照常注入）。"""
        updates = self._allow_end(_eval_messages(content))
        assert "evaluate_reminder_count" in updates

    @pytest.mark.parametrize(
        "content",
        [
            "散文：任务已完成，success 字段为 true 只是我在叙述",
            '{"success": tru}',
            '{"success": false, "metadata": {"result": "completed"}}',
            '{"success": false, "error": "boom"}',
            # retry 裁决（工具成功但评估未通过）不构成通过证据
            '{"success": true, "metadata": {"result": "retry"}}',
            # success 语义成立但无 metadata（无法证实通过）不构成通过证据
            '{"success": true}',
            '{"output": "done"}',
        ],
    )
    def test_non_evidence_payloads_do_not_allow_end(self, content: str) -> None:
        """散文/损坏 JSON/失败态/非 completed 裁决/metadata 缺席都不收束。"""
        updates = self._allow_end(_eval_messages(content))
        assert "evaluate_reminder_count" in updates
        assert "ended" not in updates

    def test_non_string_content_skipped(self) -> None:
        """多模态块等非字符串载荷跳过（无 JSON 语义）。"""
        messages = _eval_messages("", call_id="c9")
        messages[-1]["content"] = [{"type": "text", "text": "ok"}]
        updates = self._allow_end(messages)
        assert "evaluate_reminder_count" in updates

    def test_evaluate_call_without_id_not_evidence(self) -> None:
        """task_evaluate 调用缺 id 时无法与 tool 结果配对 → 不构成证据。"""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "task_evaluate"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"success": true, "metadata": {"result": "completed"}}'},
        ]
        updates = self._allow_end(messages)
        assert "evaluate_reminder_count" in updates

    def test_error_only_slim_payload_not_evidence(self) -> None:
        """slim 形态仅在「无 success 且无 error」时算成功；带 error 不算。"""
        assert _mod.TaskReminder._has_successful_task_evaluate(
            _eval_messages('{"output": "o", "error": "failed", "metadata": {"result": "completed"}}')
        ) is False
        assert _mod.TaskReminder._has_successful_task_evaluate(
            _eval_messages('{"output": "o", "metadata": {"result": "completed"}}')
        ) is True


# ═══════════════════════════════════════════════════════════
# 3. 回退检测（最后一条有文本的 assistant）
# ═══════════════════════════════════════════════════════════


class TestLastAssistantFallback:
    """_detect_last_assistant_json：只扫最后一条有文本的 assistant。"""

    def test_returns_none_on_empty_message_list(self) -> None:
        assert TaskReminder._detect_last_assistant_json([]) is None
        assert TaskReminder._detect_last_assistant_json(None) is None

    @pytest.mark.parametrize(
        "messages",
        [
            [{"role": "user", "content": _EVAL_JSON}],  # 非 assistant 带 JSON 也不看
            ["raw-string", 42, None, {"role": "tool", "content": "x"}],
        ],
    )
    def test_non_assistant_entries_skipped(self, messages: list[Any]) -> None:
        assert TaskReminder._detect_last_assistant_json(messages) is None

    def test_empty_text_assistant_skipped(self) -> None:
        """空/空白 assistant 被跳过，继续回溯到有文本的那条。"""
        messages = [
            {"role": "assistant", "content": _EVAL_JSON},
            {"role": "assistant", "content": "   "},
        ]
        detected = TaskReminder._detect_last_assistant_json(messages)
        assert detected is not None
        assert detected["passed"] is True

    def test_missing_content_key_treated_as_empty(self) -> None:
        """content 键缺失等同空文本（不因 None 崩溃）。"""
        messages = [
            {"role": "assistant", "content": _EVAL_JSON},
            {"role": "assistant"},
        ]
        assert TaskReminder._detect_last_assistant_json(messages) is not None

    def test_last_text_assistant_without_json_stops_lookup(self) -> None:
        """最后一条有文本的 assistant 无 JSON → None（更早轮次不作本轮证据）。"""
        messages = [
            {"role": "assistant", "content": _EVAL_JSON},
            {"role": "assistant", "content": "本轮只是普通回复，没有结论 JSON"},
        ]
        assert TaskReminder._detect_last_assistant_json(messages) is None

    def test_execute_falls_back_to_last_assistant_when_raw_result_bare(self) -> None:
        """评估模式：raw_result 无结论 JSON → 从最后一条 assistant 文本回扫命中。"""
        plugin = TaskReminder(config={"evaluation_mode": True})
        state = _base(
            raw_result="本轮只给了一句总结",
            messages=[{"role": "assistant", "content": _EVAL_JSON}],
        )
        updates = _run(plugin, state)
        assert updates["evaluation.detected_result"]["score"] == 88.0
        assert updates["ended"] is True

    def test_execute_uses_raw_result_when_json_present(self) -> None:
        """对照：raw_result 自带结论 JSON 时直接命中（不必回扫 messages）。"""
        plugin = TaskReminder(config={"evaluation_mode": True})
        state = _base(
            raw_result=_EVAL_JSON,
            messages=[{"role": "assistant", "content": "另一段无 JSON 文本"}],
        )
        updates = _run(plugin, state)
        assert updates["evaluation.detected_result"]["passed"] is True


# ═══════════════════════════════════════════════════════════
# 4. JSON 候选解析容错
# ═══════════════════════════════════════════════════════════


class TestEvaluationJsonCandidateTolerance:
    """花括号配对候选逐个试解析：脏候选跳过，后续合法候选仍可命中。"""

    def test_invalid_candidate_skipped_and_none_returned(self) -> None:
        """唯一候选非法（脏 JSON）→ 跳过 → 无结论返回 None。"""
        assert (
            TaskReminder._detect_evaluation_result_json("过程记录 {不是 JSON} 结束") is None
        )

    @pytest.mark.parametrize(
        ("text", "expected_passed"),
        [
            # 脏候选在前、合法候选在后（逆序尝试 → 先命中合法者）
            ('{"broken": } 修正后：{"evaluation_result": {"passed": true}}', True),
            ('{"evaluation_result": {"passed": false}} 备注 {oops}', False),
        ],
    )
    def test_valid_candidate_after_invalid_still_detected(
        self, text: str, expected_passed: bool
    ) -> None:
        """非法候选不毒化整体判定——同一文本内的合法候选照常产出结论。"""
        detected = TaskReminder._detect_evaluation_result_json(text)
        assert detected is not None
        assert detected["passed"] is expected_passed

    def test_bare_passed_payload_accepted(self) -> None:
        """无 evaluation_result 包裹、顶层带 passed 的载荷同样接受。"""
        detected = TaskReminder._detect_evaluation_result_json('{"passed": true, "score": 70}')
        assert detected is not None
        assert detected["passed"] is True

    def test_payload_without_passed_rejected(self) -> None:
        """合法 JSON 但无 passed 字段 → 不构成结论（继续找下一个候选）。"""
        assert TaskReminder._detect_evaluation_result_json('{"note": "没有结论"}') is None

    @pytest.mark.parametrize(
        "text",
        ["完全没有花括号", "", "单边 { 未闭合"],
    )
    def test_no_candidates_returns_none(self, text: str) -> None:
        """无配对候选/空文本 → None（不误报）。"""
        assert TaskReminder._detect_evaluation_result_json(text) is None

    def test_execute_survives_invalid_raw_result_candidate(self) -> None:
        """执行路径：raw_result 脏候选不阻断，回退到 assistant 文本的合法结论。"""
        plugin = TaskReminder(config={"evaluation_mode": True})
        state = _base(
            raw_result="扫描日志 {trace: broken} 完毕",
            messages=[
                {
                    "role": "assistant",
                    "content": '{"evaluation_result": {"passed": true, "score": 55}}',
                }
            ],
        )
        updates = _run(plugin, state)
        assert updates["evaluation.detected_result"]["score"] == 55.0
        assert updates["ended"] is True


# ═══════════════════════════════════════════════════════════
# 5. 活跃子任务的服务回退正路
# ═══════════════════════════════════════════════════════════


class TestActiveChildrenServiceFallback:
    """state 无标记时经 tasks.service_access 懒加载查询活跃子任务。"""

    def _install_service(self, monkeypatch: pytest.MonkeyPatch, service: Any) -> None:
        monkeypatch.setitem(
            sys.modules,
            "tasks.service_access",
            _types.SimpleNamespace(get_task_service=lambda: service),
        )

    def test_service_path_queried_and_result_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务可用（无活跃子任务）→ 以服务结果为准，正常催评估并记录查询任务。"""
        service = _FakeTaskService([])
        self._install_service(monkeypatch, service)

        updates = _run(TaskReminder(), _base())
        assert service.queried == ["task-gaps"]
        assert updates["evaluate_reminder_count"] == 1
        assert updates["_has_new_llm_input"] is True

    def test_service_path_active_child_suspends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务报出活跃子任务 → 挂起等子任务终态唤醒（状态枚举值形态可识别）。

        空放行（只跳过提醒）会让 D2 路由把纯文本轮回 llm_call 空转。
        """
        service = _FakeTaskService([_types.SimpleNamespace(id="child-1", status="running")])
        self._install_service(monkeypatch, service)

        updates = _run(TaskReminder(), _base())
        assert service.queried == ["task-gaps"]
        assert updates.get("suspended") is True
        assert updates.get("_has_new_llm_input") is False
        assert "evaluate_reminder_count" not in updates

    def test_state_marker_short_circuits_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """state 有 submitted_task_ids 标记时不再查服务（读取顺序契约）。

        标记在场 = 读面不可用降级下仍有活跃子任务 → 同样挂起（fail-closed：
        降级链不得把「可能有子任务在飞」放行成回 LLM 空转）。
        """
        service = _FakeTaskService([])
        self._install_service(monkeypatch, service)

        updates = _run(TaskReminder(), _base(submitted_task_ids=["child-1"]))
        assert service.queried == []
        assert updates.get("suspended") is True
        assert "evaluate_reminder_count" not in updates

    def test_service_unavailable_falls_back_to_reminder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务不可用（模块不可导入）→ 视为无活跃子任务，不抛。"""
        monkeypatch.setitem(sys.modules, "tasks.service_access", None)
        updates = _run(TaskReminder(), _base())
        assert updates["evaluate_reminder_count"] == 1


# ═══════════════════════════════════════════════════════════
# 6. server.py 适配层 dict 直通
# ═══════════════════════════════════════════════════════════


class TestServerDictPassthrough:
    """插件返回 core 型 dict 时适配层原样回传（双形态契约的 core 侧）。"""

    def _server(self) -> Any:
        return _load_module("task_reminder_gaps_server_test", "server.py")

    def test_core_dict_result_returned_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = self._server()

        class _CorePlugin:
            async def execute(self, ctx: Any) -> dict[str, Any]:
                return {"task.status": "running", "core": True}

        monkeypatch.setattr(srv, "get_instance", _CorePlugin)
        resp = asyncio.run(srv.execute(state={"task.id": "t"}))
        assert resp == {"task.status": "running", "core": True}
        assert "state_updates" not in resp

    def test_output_result_still_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：OutputResult 走展开形态（dict 分支不误吞正常信封）。"""
        srv = self._server()
        from _libs.pipeline_base.plugin import OutputResult

        class _OutputPlugin:
            async def execute(self, ctx: Any) -> OutputResult:
                return OutputResult(state_updates={"a": 1}, skip_remaining=True)

        monkeypatch.setattr(srv, "get_instance", _OutputPlugin)
        resp = asyncio.run(srv.execute(state={"task.id": "t"}))
        assert resp == {"state_updates": {"a": 1}, "skip_remaining": True}


# ═══════════════════════════════════════════════════════════
# 7. 失败写点复查面的 DB 权威终态（db_completed 参数契约）
# ═══════════════════════════════════════════════════════════


class TestDbCompletedExhaustionGuard:
    """复查条件 = ``_has_completion_evidence(state) or db_completed``。

    execute 全路径上 DB completed 会先被信号②（扩展后含 DB 位）当轮收束，
    两个耗尽助手的 ``db_completed=True`` 分支按契约以直接调用覆盖（同本文件头
    「经 execute 不可达的行」先例）；信号②位与 reader 注入的全路径场景见
    test_task_reminder_three_signal.py。
    """

    def _exhausted_updates(self, db_completed: bool) -> dict[str, Any]:
        return dict(
            TaskReminder()._reminder_exhausted_result(
                _base(),
                reminder_count=3,
                iteration=4,
                task_id="task-gaps",
                db_completed=db_completed,
            ).state_updates
        )

    def test_db_completed_prevents_failed_write(self) -> None:
        """DB completed 在场 → 不写 failed（终态不可覆盖），保持现状 end 本轮。"""
        updates = self._exhausted_updates(db_completed=True)
        assert "task.status" not in updates
        assert "task.ended_at" not in updates
        assert updates["ended"] is True
        assert updates["_has_new_llm_input"] is False

    def test_db_not_completed_writes_failed(self) -> None:
        """对照：db_completed=False → failed 裁决照旧（含失败归因三键）。"""
        updates = self._exhausted_updates(db_completed=False)
        assert updates["task.status"] == "failed"
        assert updates["task.failure_class"] == "reminder_exhausted"
        assert updates["ended"] is True

    def test_empty_exhausted_db_completed_preserves_status(self) -> None:
        """空回复耗尽同面：DB completed → 不写 failed，仍收束 end。"""
        updates = TaskReminder()._empty_exhausted_result(
            _base(raw_result="", llm_empty_exhausted=True),
            iteration=4,
            task_id="task-gaps",
            db_completed=True,
        ).state_updates
        assert "task.status" not in updates
        assert "task.ended_at" not in updates
        assert updates["ended"] is True


# ═══════════════════════════════════════════════════════════
# 8. _db_task_status 读面契约（本管道聚合行 task_id==pipeline_id）
# ═══════════════════════════════════════════════════════════


class TestDbTaskStatusReader:
    """DB 权威终态读取：诚实降级面逐项锁定（None = 行为退化为现状）。"""

    async def _status(self, monkeypatch: pytest.MonkeyPatch, reader: Any) -> Any:
        monkeypatch.setattr(_mod, "_get_state_reader", lambda: reader)
        return await TaskReminder()._db_task_status("task-gaps")

    async def test_matching_row_status_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本管道聚合行（pipeline_id==task_id）的 task.status 原样返回。"""
        rows = [
            {"pipeline_id": "other", "task.status": "completed"},
            {"pipeline_id": "task-gaps", "task.status": "completed"},
        ]
        assert await self._status(monkeypatch, rows) == "completed"

    async def test_reader_none_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """reader 未注入 → None（诚实降级，不抛）。"""
        assert await self._status(monkeypatch, None) is None

    async def test_row_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """聚合行缺失（无本管道行）→ None。"""
        rows = [{"pipeline_id": "other", "task.status": "completed"}]
        assert await self._status(monkeypatch, rows) is None

    async def test_row_without_status_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """行在但无 task.status（评估者模式管道自身行形态）→ None。"""
        assert await self._status(monkeypatch, [{"pipeline_id": "task-gaps"}]) is None

    async def test_reader_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读面异常 → None（读面降级不崩插件步，与活跃子任务读面同一容错）。"""

        def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("bridge down")

        assert await self._status(monkeypatch, _boom) is None
