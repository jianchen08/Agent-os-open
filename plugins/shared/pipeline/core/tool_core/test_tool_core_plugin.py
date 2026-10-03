# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""tool_core 单调用契约的行为测试。

mock 两个 capability delegate（tool-executor / event-bus），不连内核；
断言可观察输出（事件序列 / state_updates / messages ops），不断言内部实现。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from plugin import ToolCore, truncate_tool_output
from agentos_plugin_sdk.pipeline_types import PluginContext, create_initial_state


class FakeToolExecutor:
    """tool-executor 委托伪实现：记录调用并返回预置信封。"""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.timeouts: list[float | None] = []
        self.response = response if response is not None else {"success": True, "data": {"ok": True}}

    async def __call__(self, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(params)
        self.timeouts.append(timeout)
        return self.response

    @property
    def invoke_count(self) -> int:
        return len(self.calls)


class FakeEventBus:
    """event-bus 委托伪实现：记录事件发射序。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, params: dict[str, Any]) -> None:
        self.events.append(params)


def _ctx(state: dict[str, Any], config: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=create_initial_state(**state), config=config or {})


def _plugin(
    tool_response: dict[str, Any] | None = None,
) -> tuple[ToolCore, FakeToolExecutor, FakeEventBus]:
    core = ToolCore()
    executor = FakeToolExecutor(tool_response)
    event_bus = FakeEventBus()
    core.set_tool_delegate(executor)
    core.set_event_delegate(event_bus)
    return core, executor, event_bus


def _event_names(event_bus: FakeEventBus) -> list[str]:
    return [e["event"] for e in event_bus.events]


# ── ① 正常执行成功 ────────────────────────────────────────────────


def test_successful_call_events_collect_keys_and_message_ops() -> None:
    core, executor, event_bus = _plugin({"success": True, "data": {"output": "hi"}})
    state = {
        "current_call": {"name": "bash_execute", "id": "call_1", "args": {"command": "echo hi"}},
        "session_id": "sess-1",
        "pipeline_id": "pipe-1",
        "message_id": "msg-1",
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash_execute", "arguments": "{}"}}]}
        ],
    }
    updates = asyncio.run(core.execute(_ctx(state)))

    # 事件序列：tool_start → tool_result。
    assert _event_names(event_bus) == ["tool_start", "tool_result"]
    start_payload = event_bus.events[0]["payload"]
    assert start_payload["tool_name"] == "bash_execute"
    assert start_payload["call_id"] == "call_1"
    assert start_payload["thread_id"] == "sess-1"
    result_payload = event_bus.events[1]["payload"]
    assert result_payload["success"] is True
    assert "hi" in result_payload["result"]
    assert result_payload["result_data"] == {"output": "hi"}
    assert "error" not in result_payload

    # invoke 参数：单调用 + _call_context 路由键；timeout 传大值（长任务语义）。
    assert executor.invoke_count == 1
    assert executor.calls[0]["tool_name"] == "bash_execute"
    assert executor.calls[0]["args"] == {"command": "echo hi"}
    assert executor.calls[0]["_call_context"] == {
        "call_id": "call_1",
        "pipeline_id": "pipe-1",
        "message_id": "msg-1",
        "thread_id": "sess-1",
    }
    assert executor.timeouts[0] == 600.0

    # collect 键各为单元素数组。
    assert len(updates["tool_results"]) == 1
    assert updates["tool_results"][0]["success"] is True
    assert updates["tool_results"][0]["tool_name"] == "bash_execute"
    assert updates["_full_tool_results"] == [{"tool_name": "bash_execute", "success": True, "error": None, "data": {"output": "hi"}, "metadata": None, "duration_ms": updates["_full_tool_results"][0]["duration_ms"]}]
    assert updates["_executed_tool_calls"] == [{"name": "bash_execute", "id": "call_1", "args": {"command": "echo hi"}}]

    # messages op 经 SDK 构造器产出：快照已有 assistant 配对消息 → 仅 tool op。
    ops = updates["messages"]["_ops"]
    assert len(ops) == 1
    assert ops[0]["op"] == "set"
    msg = ops[0]["msg"]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_1"
    envelope = msg["tool_result"]
    assert set(envelope) == {"call_id", "tool_name", "success", "error", "data", "metadata", "duration_ms"}
    assert envelope["call_id"] == "call_1"

    # raw_result 预览 ≤ 200 字符（成功 = serialize 后取前 200）。
    assert isinstance(updates["raw_result"], str)
    assert len(updates["raw_result"]) <= 200


def test_raw_result_preview_truncated_at_200_chars() -> None:
    core, _, _ = _plugin({"success": True, "data": {"output": "x" * 500}})
    state = {
        "current_call": {"name": "f", "id": "c1"},
        "messages": [{"role": "assistant", "tool_calls": [{"id": "c1"}]}],
    }
    updates = asyncio.run(core.execute(_ctx(state)))
    assert len(updates["raw_result"]) == 200


def test_assistant_message_synthesized_when_missing() -> None:
    core, _, _ = _plugin()
    state = {"current_call": {"name": "f", "id": "c9", "args": {"a": 1}}, "raw_thinking": "think"}
    ops = asyncio.run(core.execute(_ctx(state)))["messages"]["_ops"]
    assert [op["msg"]["role"] for op in ops] == ["assistant", "tool"]
    assistant = ops[0]["msg"]
    assert assistant["tool_calls"][0]["id"] == "c9"
    assert assistant["tool_calls"][0]["function"]["name"] == "f"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
    assert assistant["reasoning_content"] == "think"


# ── ② 超长输出截断（边界对：恰好阈值 / 超阈值）──────────────────

CONTRACT_MAX = 16_384
CONTRACT_HEAD = 12_288
CONTRACT_TAIL = 2_048
TRUNCATION_MARKER = "工具输出已截断"


@pytest.mark.parametrize(
    ("total", "truncated"),
    [
        (CONTRACT_MAX, False),  # 恰好契约上限不触发（闭区间阈值）
        (CONTRACT_MAX + 1, True),  # 超限 1 字符即触发
        (CONTRACT_MAX + 5000, True),
    ],
)
def test_output_truncation_boundary(total: int, truncated: bool) -> None:
    core, _, _ = _plugin({"success": True, "data": {"blob": "x" * total}})
    state = {"current_call": {"name": "f", "id": "c1"}}
    updates = asyncio.run(core.execute(_ctx(state)))

    display_data = updates["tool_results"][0]["data"]
    full_data = updates["_full_tool_results"][0]["data"]
    assert full_data["blob"] == "x" * total, "全文留档不截断"
    if not truncated:
        assert display_data["blob"] == "x" * total
    else:
        shown = display_data["blob"]
        assert TRUNCATION_MARKER in shown
        assert shown.startswith("x") and shown.endswith("x"), "头尾保留"
        assert len(shown) < total
        head = shown[:CONTRACT_HEAD]
        assert head == "x" * CONTRACT_HEAD
        omitted = int(shown.split("省略 ")[1].split(" 字符")[0])
        assert omitted == total - CONTRACT_HEAD - CONTRACT_TAIL, "省略数 = total − head − tail"


def test_truncate_tool_output_short_text_untouched() -> None:
    limits = {"max_chars": 100, "head_chars": 60, "tail_chars": 20}
    assert truncate_tool_output("hello", limits) == "hello"


# ── ③④ 预填短路 ─────────────────────────────────────────────────


def test_pre_decided_hit_by_call_id_skips_execution() -> None:
    core, executor, event_bus = _plugin()
    state = {
        "current_call": {"name": "bash_execute", "id": "call_x", "args": {"command": "ls"}},
        "pre_decided_results": [
            {
                "call_id": "call_x",
                "tool_name": "bash_execute",
                "success": False,
                "error": "Agent level L1 not allowed to call: bash_execute",
                "data": None,
                "metadata": {"decided_by": "level_guard"},
                "duration_ms": 0.0,
            }
        ],
        "messages": [{"role": "assistant", "tool_calls": [{"id": "call_x"}]}],
    }
    updates = asyncio.run(core.execute(_ctx(state)))

    # 幂等核心断言：有结果就不执行。
    assert executor.invoke_count == 0
    # 事件照发（start + result，与执行路径 UX 一致）。
    assert _event_names(event_bus) == ["tool_start", "tool_result"]
    assert event_bus.events[1]["payload"]["success"] is False
    assert "L1" in event_bus.events[1]["payload"]["error"]

    results = updates["tool_results"]
    assert len(results) == 1
    assert results[0]["success"] is False
    assert "L1" in results[0]["error"]
    assert results[0]["metadata"] == {"decided_by": "level_guard"}
    # 预填不清写 pre_decided_results（引擎 consume_keys 负责）。
    assert "pre_decided_results" not in updates
    # 全文留档同含预填结果。
    assert len(updates["_full_tool_results"]) == 1


def test_pre_decided_extras_passthrough_to_tool_results() -> None:
    core, executor, _ = _plugin()
    state = {
        "current_call": {"name": "bash_execute", "id": "call_r"},
        "pre_decided_results": [
            {
                "call_id": "call_r",
                "tool_name": "bash_execute",
                "success": False,
                "error": "审批通道故障",
                "retry_allowed": True,
                "arguments": {"command": "ls"},
            }
        ],
    }
    updates = asyncio.run(core.execute(_ctx(state)))
    assert executor.invoke_count == 0
    result = updates["tool_results"][0]
    assert result["retry_allowed"] is True, "扩展位随 tool_results 透传"
    assert result["arguments"] == {"command": "ls"}
    # envelope 七键封闭：配对消息不带扩展位。
    tool_msg = updates["messages"]["_ops"][-1]["msg"]
    assert "retry_allowed" not in tool_msg["tool_result"]


def test_pre_decided_name_fallback_for_idless_call() -> None:
    core, executor, _ = _plugin()
    state = {
        "current_call": {"name": "file_write", "args": {"path": "x"}},
        "pre_decided_results": [
            {
                "tool_name": "file_write",
                "success": False,
                "error": "工具被权限策略拦截: 幻觉调用",
                "metadata": {"decided_by": "level_guard"},
                "duration_ms": 0.0,
            }
        ],
    }
    updates = asyncio.run(core.execute(_ctx(state)))
    assert executor.invoke_count == 0
    result = updates["tool_results"][0]
    assert result["success"] is False, "无 id 调用按工具名兜底命中"
    assert "幻觉调用" in result["error"]


def test_pre_decided_success_defaults_fail_closed() -> None:
    core, executor, _ = _plugin()
    state = {
        "current_call": {"name": "bash_execute", "id": "call_z"},
        "pre_decided_results": [{"call_id": "call_z", "data": {"x": 1}}],
    }
    updates = asyncio.run(core.execute(_ctx(state)))
    assert executor.invoke_count == 0
    result = updates["tool_results"][0]
    assert result["success"] is False, "success 缺省 fail-closed"
    assert result["tool_name"] == "bash_execute", "tool_name 缺省回退调用名"
    assert result["data"] == {"x": 1}


# ── ⑤ args JSON 无效 ─────────────────────────────────────────────


def test_invalid_args_json_fails_with_split_advice() -> None:
    core, executor, event_bus = _plugin()
    state = {"current_call": {"name": "file_write", "id": "c1", "args": '{"path": "a.txt", "content": '}}
    updates = asyncio.run(core.execute(_ctx(state)))

    assert executor.invoke_count == 0, "解析失败不进执行面"
    assert _event_names(event_bus) == [], "解析失败不发事件（对齐 Rust 蓝本）"
    result = updates["tool_results"][0]
    assert result["success"] is False
    assert "拆分为多个小步骤" in result["error"]
    assert result["duration_ms"] == 0.0
    # 调用仍留档（整形面完整）。
    assert updates["_executed_tool_calls"] == [{"name": "file_write", "id": "c1", "args": '{"path": "a.txt", "content": '}]


def test_args_json_string_parsed_and_non_object_args_normalized() -> None:
    core, executor, _ = _plugin()
    state = {"current_call": {"name": "f", "id": "c1", "args": '{"a": 1}'}}
    asyncio.run(core.execute(_ctx(state)))
    assert executor.calls[0]["args"] == {"a": 1}, "JSON 字符串参数解析后执行"

    core2, executor2, _ = _plugin()
    state2 = {"current_call": {"name": "f", "id": "c2", "args": [1, 2]}}
    asyncio.run(core2.execute(_ctx(state2)))
    assert executor2.calls[0]["args"] == {}, "非对象 args 归一空对象"


# ── ⑥ 输出契约违规 fail-closed ───────────────────────────────────


def test_output_schema_violation_fails_closed() -> None:
    core, executor, event_bus = _plugin(
        {"success": True, "data": {"exit_code": 0}}  # 缺 required 字段 status
    )
    state = {
        "current_call": {"name": "bash_execute", "id": "c1"},
        "tool_output_validation": "on",
        "tool_output_contracts": {
            "bash_execute": {
                "schema": {
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                },
            }
        },
    }
    updates = asyncio.run(core.execute(_ctx(state)))

    result = updates["tool_results"][0]
    assert result["success"] is False, "违规 fail-closed"
    assert "output_schema validation failed" in result["error"]
    # 校验发生在 tool_result 事件前：事件里的 success/error 与持久化同源。
    result_payload = event_bus.events[-1]["payload"]
    assert result_payload["success"] is False
    assert "output_schema validation failed" in result_payload["error"]


def test_output_schema_pass_and_off_switch() -> None:
    response = {"success": True, "data": {"status": "completed"}}
    state = {
        "current_call": {"name": "bash_execute", "id": "c1"},
        "tool_output_contracts": {
            "bash_execute": {"schema": {"type": "object", "required": ["status"]}}
        },
    }
    core, executor, _ = _plugin(response)
    updates = asyncio.run(core.execute(_ctx(dict(state))))
    assert updates["tool_results"][0]["success"] is True, "合规放行"
    assert executor.invoke_count == 1

    # 开关关闭（tool_output_validation == "off"）整体跳过：违规数据也放行。
    core2, _, _ = _plugin({"success": True, "data": {"missing": True}})
    state_off = dict(state)
    state_off["tool_output_validation"] = "off"
    updates2 = asyncio.run(core2.execute(_ctx(state_off)))
    assert updates2["tool_results"][0]["success"] is True


# ── ⑦ 任务系统级失败 ─────────────────────────────────────────────


def test_task_failed_metadata_sets_ended_and_raw_error() -> None:
    core, _, _ = _plugin(
        {
            "success": True,
            "data": {"error": "任务系统级失败: 内核不可用", "metadata": {"task_failed": True}},
        }
    )
    state = {"current_call": {"name": "task_status", "id": "c1"}}
    updates = asyncio.run(core.execute(_ctx(state)))
    assert updates["ended"] is True
    assert updates["raw_error"] == "任务系统级失败: 内核不可用"


def test_normal_success_does_not_write_raw_error() -> None:
    core, _, _ = _plugin()
    state = {"current_call": {"name": "f", "id": "c1"}}
    updates = asyncio.run(core.execute(_ctx(state)))
    assert "raw_error" not in updates, "平时不写 raw_error（并行归并 last-wins）"
    assert "ended" not in updates


# ── ⑧ submitted_task_ids 增量去重 ────────────────────────────────


def _submit_state(task_id_in_snapshot: bool) -> dict[str, Any]:
    state: dict[str, Any] = {
        "current_call": {"name": "task_submit", "id": "c1"},
    }
    if task_id_in_snapshot:
        state["submitted_task_ids"] = ["t-1"]
    return state


def test_submitted_task_ids_delta_dedup() -> None:
    response = {
        "success": True,
        "data": {"task_id": "t-1", "workspace": "/ws"},
        "metadata": {"action": "task_submit"},
    }
    # 快照已有 → 增量键不写。
    core, _, _ = _plugin(response)
    updates = asyncio.run(core.execute(_ctx(_submit_state(task_id_in_snapshot=True))))
    assert "submitted_task_ids" not in updates, "快照已有的 id 不重复写增量"

    # 快照没有 → 写单元素增量。
    core2, _, _ = _plugin(response)
    updates2 = asyncio.run(core2.execute(_ctx(_submit_state(task_id_in_snapshot=False))))
    assert updates2["submitted_task_ids"] == ["t-1"]
    assert updates2["task_id"] == "t-1", "快照缺 task_id 时落 latest"
    assert updates2["workspace"] == "/ws"


def test_submitted_task_keys_not_overwritten_when_snapshot_has_them() -> None:
    response = {
        "success": True,
        "data": {"task_id": "t-2", "workspace": "/ws-2"},
        "metadata": {"action": "task_submit_container"},
    }
    core, _, _ = _plugin(response)
    state = {
        "current_call": {"name": "task_submit", "id": "c1"},
        "submitted_task_ids": [],
        "task_id": "t-0",
        "workspace": "/ws-0",
    }
    updates = asyncio.run(core.execute(_ctx(state)))
    assert updates["submitted_task_ids"] == ["t-2"]
    assert "task_id" not in updates and "workspace" not in updates, "快照已有键不覆盖"


def test_task_evaluate_completed_signal() -> None:
    core, _, _ = _plugin(
        {"success": True, "data": {"task_id": "t-1"}, "metadata": {"result": "completed"}}
    )
    state = {"current_call": {"name": "task_evaluate", "id": "c1"}}
    updates = asyncio.run(core.execute(_ctx(state)))
    assert updates["task_evaluation_completed"] is True


def test_human_interaction_route_signal() -> None:
    core, _, _ = _plugin({"success": True, "data": {"conversation_mode": True}})
    state = {"current_call": {"name": "human_interaction", "id": "c1"}}
    updates = asyncio.run(core.execute(_ctx(state)))
    assert updates["_pending_route_signal"] == {
        "route_type": "wait",
        "reason": "human_interaction: user arrived, entering conversation",
    }


# ── ⑨ no-op ──────────────────────────────────────────────────────


@pytest.mark.parametrize("current_call", [None, {}, "not-a-dict"])
def test_missing_current_call_is_noop(current_call: Any) -> None:
    core, executor, event_bus = _plugin()
    updates = asyncio.run(core.execute(_ctx({"current_call": current_call})))
    assert updates == {}
    assert executor.invoke_count == 0
    assert event_bus.events == []


# ── 执行失败路径 ─────────────────────────────────────────────────


def test_executor_failure_error_text_and_event_contract() -> None:
    core, _, event_bus = _plugin({"success": False, "error": "command not found"})
    state = {"current_call": {"name": "bash_execute", "id": "c1"}}
    updates = asyncio.run(core.execute(_ctx(state)))

    result = updates["tool_results"][0]
    assert result["success"] is False
    assert result["error"] == "command not found"
    payload = event_bus.events[-1]["payload"]
    assert payload["success"] is False
    assert payload["error"] == "command not found", "流式契约 error 必须 string"
    assert payload["result"] == "Error: command not found"
    assert "result_data" not in payload, "失败路径 data 为 None，result_data 缺席"
    assert updates["raw_result"] == "Error: command not found"


def test_executor_envelope_missing_error_defaults() -> None:
    core, _, _ = _plugin({"success": False})
    state = {"current_call": {"name": "f", "id": "c1"}}
    updates = asyncio.run(core.execute(_ctx(state)))
    assert updates["tool_results"][0]["error"] == "tool execution failed"


def test_delegate_not_injected_fails_explicitly() -> None:
    core = ToolCore()
    core.set_event_delegate(FakeEventBus())
    updates = asyncio.run(core.execute(_ctx({"current_call": {"name": "f", "id": "c1"}})))
    result = updates["tool_results"][0]
    assert result["success"] is False
    assert result["error"] == "host capability call unavailable"


def test_delegate_exception_becomes_failed_result() -> None:
    core = ToolCore()

    async def boom(params: dict, timeout: float | None = None) -> dict:
        raise RuntimeError("channel closed")

    core.set_tool_delegate(boom)
    core.set_event_delegate(FakeEventBus())
    updates = asyncio.run(core.execute(_ctx({"current_call": {"name": "f", "id": "c1"}})))
    result = updates["tool_results"][0]
    assert result["success"] is False
    assert "host capability call failed" in result["error"]


# ── 多模态 ───────────────────────────────────────────────────────


def test_multimodal_images_emitted_and_user_message_injected() -> None:
    core, _, event_bus = _plugin(
        {
            "success": True,
            "data": {"base64_data": "AAAA", "mime_type": "image/png", "path": "/s/a.png"},
            "metadata": {
                "multimodal_content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}}
                ]
            },
        }
    )
    state = {
        "current_call": {"name": "screenshot", "id": "c1"},
        "llm_supports_vision": True,
    }
    updates = asyncio.run(core.execute(_ctx(state)))

    kinds = [e["event"] for e in event_bus.events]
    assert "tool_multimedia_result" in kinds
    mm = next(e for e in event_bus.events if e["event"] == "tool_multimedia_result")
    assert mm["payload"]["count"] == 2
    assert mm["payload"]["multimedia"][0] == {"mime_type": "image/png", "path": "/s/a.png"}
    assert all("base64" not in item for item in mm["payload"]["multimedia"]), "事件不含 base64"

    ops = updates["messages"]["_ops"]
    user_msg = next(op["msg"] for op in ops if op["msg"].get("role") == "user")
    assert user_msg["name"] == "tool_images"
    blocks = user_msg["content"]
    assert blocks[0]["type"] == "text"
    assert sum(1 for b in blocks if b["type"] == "image_url") == 2
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_multimodal_without_vision_guides_text_analysis() -> None:
    core, _, _ = _plugin(
        {"success": True, "data": {"images": [{"base64": "CCCC", "path": "/s/b.png"}]}}
    )
    state = {"current_call": {"name": "screenshot", "id": "c1"}, "llm_supports_vision": False}
    ops = asyncio.run(core.execute(_ctx(state)))["messages"]["_ops"]
    user_msg = next(op["msg"] for op in ops if op["msg"].get("role") == "user")
    assert "不支持图片分析" in user_msg["content"]
    assert "/s/b.png" in user_msg["content"]


# ── 事件 duration 与超时配置 ─────────────────────────────────────


def test_invoke_timeout_from_config() -> None:
    core = ToolCore(config={"tool_output": {"invoke_timeout_s": 30}})
    executor = FakeToolExecutor()
    core.set_tool_delegate(executor)
    state = {"current_call": {"name": "f", "id": "c1"}}
    asyncio.run(core.execute(_ctx(state, config={"tool_output": {"invoke_timeout_s": 30}})))
    assert executor.timeouts[0] == 30.0


def test_event_result_text_truncated_for_huge_payload() -> None:
    core, _, event_bus = _plugin({"success": True, "data": {"blob": "y" * 50_000}})
    state = {"current_call": {"name": "f", "id": "c1"}}
    asyncio.run(core.execute(_ctx(state)))
    payload = event_bus.events[-1]["payload"]
    assert TRUNCATION_MARKER in payload["result"], "事件 result 文本按限长截断"
    assert len(payload["result"]) < 50_000
