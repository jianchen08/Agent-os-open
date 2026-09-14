# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""task_reminder 插件剩余分支补测（缺行清零批）。

锁定以下行为契约（对应 plugin.py / server.py 缺行）：

1. **空回复耗尽的完成证据复查**（599）：``llm_empty_exhausted`` 且任务身份在场时，
   ``evaluation.detected_result`` 是完成证据——终态不被覆盖为 failed，仍收束 end
   （与提醒耗尽同一复查面，已完成态不可覆盖）。
2. **完成证据全集**（497）：``_has_completion_evidence`` = state 完成证据 ∪
   evaluation.detected_result；state 侧命中即 True（短路，不再看 detected_result）。
3. **次级证据的结构化判定**（663/674/689）——``_has_successful_task_evaluate``
   只认「task_evaluate 调用 id 配对 + JSON 载荷」的证据：
   - 未配对的 role=tool 消息跳过（不构成证据，哪怕 ID 相近）；
   - JSON 解析成非 dict（列表/标量）跳过（无 success 语义）；
   - slim 形态（无 success 键且无 error）视为成功——SDK toLLM 面默认序列化，
     只认 success===true 会让评估成功永远识别不到（无限提醒循环）。
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
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in (_DIR, _SHARED):
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

from pipeline.plugin import PluginContext  # noqa: E402


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
    """messages role=tool JSON 载荷的精确判定（非 JSON/非 dict/成功两形态）。"""

    def _allow_end(self, messages: list[Any]) -> dict[str, Any]:
        """次级证据命中时的可观察结果：_has_new_llm_input=False（放行结束）。"""
        return _run(TaskReminder(), _base(messages=messages))

    @pytest.mark.parametrize(
        "content",
        [
            '{"success": true}',  # 全量形态
            '{"output": {"action": "auto_complete"}}',  # slim 形态（无 success 键）
        ],
    )
    def test_successful_evidence_allows_end(self, content: str) -> None:
        """两种成功形态都构成证据 → 放行结束（不注入提醒）。"""
        updates = self._allow_end(_eval_messages(content))
        assert updates.get("_has_new_llm_input") is False
        assert "evaluate_reminder_count" not in updates

    def test_unpaired_tool_message_skipped_then_paired_hit(self) -> None:
        """未配对的 role=tool 消息被跳过；配对的 slim 成功证据仍命中。"""
        messages = [
            {"role": "tool", "tool_call_id": "unrelated-id", "content": '{"success": true}'},
            {"role": "assistant", "content": "无工具调用"},
        ] + _eval_messages('{"output": "done"}')
        updates = self._allow_end(messages)
        assert updates.get("_has_new_llm_input") is False

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
            '{"success": false}',
            '{"success": false, "error": "boom"}',
        ],
    )
    def test_non_evidence_payloads_do_not_allow_end(self, content: str) -> None:
        """散文/损坏 JSON/失败载荷都不放行（子串命中与失败态均非证据）。"""
        updates = self._allow_end(_eval_messages(content))
        assert "evaluate_reminder_count" in updates

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
            {"role": "tool", "tool_call_id": "c1", "content": '{"success": true}'},
        ]
        updates = self._allow_end(messages)
        assert "evaluate_reminder_count" in updates

    def test_error_only_slim_payload_not_evidence(self) -> None:
        """slim 形态仅在「无 success 且无 error」时算成功；带 error 不算。"""
        assert _mod.TaskReminder._has_successful_task_evaluate(
            _eval_messages('{"output": "o", "error": "failed"}')
        ) is False
        assert _mod.TaskReminder._has_successful_task_evaluate(
            _eval_messages('{"output": "o"}')
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
        """对照：服务报出活跃子任务 → 本轮跳过提醒（状态枚举值形态可识别）。"""
        service = _FakeTaskService([_types.SimpleNamespace(status="running")])
        self._install_service(monkeypatch, service)

        updates = _run(TaskReminder(), _base())
        assert service.queried == ["task-gaps"]
        assert "evaluate_reminder_count" not in updates

    def test_state_marker_short_circuits_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """state 有 submitted_task_ids 标记时不再查服务（读取顺序契约）。"""
        service = _FakeTaskService([])
        self._install_service(monkeypatch, service)

        updates = _run(TaskReminder(), _base(submitted_task_ids=["child-1"]))
        assert service.queried == []
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
        from pipeline.plugin import OutputResult

        class _OutputPlugin:
            async def execute(self, ctx: Any) -> OutputResult:
                return OutputResult(state_updates={"a": 1}, skip_remaining=True)

        monkeypatch.setattr(srv, "get_instance", _OutputPlugin)
        resp = asyncio.run(srv.execute(state={"task.id": "t"}))
        assert resp == {"state_updates": {"a": 1}, "skip_remaining": True}
