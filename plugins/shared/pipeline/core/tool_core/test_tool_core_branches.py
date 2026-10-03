# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""tool_core 分支矩阵测试——全部经公开 execute() 驱动的行为分支。

覆盖面（与 test_tool_core_plugin.py 的契约测试互补，防分支漏测）：
- 输出 schema 校验器分支矩阵：type 各型/类型列表/未知型、enum、required、
  properties 递归、items 递归、整值浮点、契约形状残缺放行、开关关闭；
- 错误信封串化三分支（str 直传 / dict 取 message / 其他 json 兜底）；
- 多模态三来源收集（base64_data / images[] / metadata data:URL）+ 残缺跳过
  + 视觉开/关两条消息路径 + tool_multimedia_result 事件；
- 消息 op：assistant 补造（快照缺配对消息）、output_truncated 注记（lines
  有无 × file_write 续写引导）；
- 副作用：task_evaluate 评估完成证据、human_interaction 会话模式（直接/
  嵌套 output.data 两形态）、raw_result 预览两形态；
- 兜底：事件委托抛异常不阻断、tool 委托返回非 dict、预填按工具名兜底 +
  扩展位透传。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load_plugin_module() -> Any:
    """按唯一模块名加载 plugin.py（平铺布局防裸名互劫持）。"""
    sys.modules.pop("plugin", None)
    mod_name = "tool_core_plugin_branches"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_plugin_module()
ToolCore = _MOD.ToolCore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class Harness:
    """脚本化双委托（tool/event 均可编程），隔离构建一个 ToolCore。"""

    def __init__(self, tool_response: Any = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.tool_params: list[dict[str, Any]] = []
        self._tool_response = tool_response
        self._plugin = ToolCore(config={})

        async def tool_delegate(params: dict, timeout: float | None = None) -> Any:
            self.tool_params.append(params)
            if isinstance(self._tool_response, BaseException):
                raise self._tool_response
            return self._tool_response or {"success": True, "data": {"output": "ok"}}

        async def event_delegate(params: dict) -> None:
            self.events.append(params)

        self._plugin.set_tool_delegate(tool_delegate)
        self._plugin.set_event_delegate(event_delegate)

    def call(self, state: dict[str, Any]) -> dict[str, Any]:
        state = {"messages": [], "session_id": "s1", "pipeline_id": "p1", **state}
        state.setdefault("current_call", {"name": "bash", "id": "c1", "args": {}})
        from pipeline.plugin import PluginContext

        ctx = PluginContext(state=state, config={})
        return _run(self._plugin.execute(ctx))


def _tool_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(e for e in events if e.get("event") == "tool_result")


# ── 输出 schema 校验器分支矩阵 ──────────────────────────────────


def _validator_state(schema: Any, *, enabled: Any = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "tool_output_contracts": {"bash": {"schema": schema, "render": "table"}},
        "pre_decided_results": [],
    }
    if enabled is not None:
        state["tool_output_validation"] = enabled
    return state


class TestOutputValidator:
    def test_type_mismatch_fails_closed(self) -> None:
        updates = Harness({"success": True, "data": "scalar"}).call(
            _validator_state({"type": "object"})
        )
        assert updates["tool_results"][0]["success"] is False
        assert "expected type" in updates["tool_results"][0]["error"]

    def test_enum_violation_and_match(self) -> None:
        schema = {"type": "string", "enum": ["a", "b"]}
        h = Harness({"success": True, "data": "c"})
        updates = h.call(_validator_state(schema))
        assert updates["tool_results"][0]["success"] is False
        assert "not in enum" in updates["tool_results"][0]["error"]
        updates_ok = Harness({"success": True, "data": "b"}).call(_validator_state(schema))
        assert updates_ok["tool_results"][0]["success"] is True

    def test_required_missing(self) -> None:
        schema = {"type": "object", "required": ["k"], "properties": {"k": {"type": "string"}}}
        updates = Harness({"success": True, "data": {}}).call(_validator_state(schema))
        assert updates["tool_results"][0]["success"] is False
        assert "missing required field `k`" in updates["tool_results"][0]["error"]

    def test_nested_property_recursion(self) -> None:
        schema = {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {"n": {"type": "integer"}}}},
        }
        bad = Harness({"success": True, "data": {"inner": {"n": "x"}}})
        assert bad.call(_validator_state(schema))["tool_results"][0]["success"] is False
        good = Harness({"success": True, "data": {"inner": {"n": 3}}})
        assert good.call(_validator_state(schema))["tool_results"][0]["success"] is True

    def test_items_recursion(self) -> None:
        schema = {"type": "array", "items": {"type": "number"}}
        updates = Harness({"success": True, "data": [1, "x"]}).call(_validator_state(schema))
        assert updates["tool_results"][0]["success"] is False

    def test_integer_accepts_whole_float_rejects_frac(self) -> None:
        schema = {"type": "integer"}
        assert (
            Harness({"success": True, "data": 1.0}).call(_validator_state(schema))[
                "tool_results"
            ][0]["success"]
            is True
        )
        updates = Harness({"success": True, "data": 1.5}).call(_validator_state(schema))
        assert updates["tool_results"][0]["success"] is False

    def test_unknown_type_name_unconstrained_and_type_list(self) -> None:
        # 未知类型名不约束（宽松）。
        updates = Harness({"success": True, "data": 42}).call(
            _validator_state({"type": "decimal80"})
        )
        assert updates["tool_results"][0]["success"] is True
        # 类型列表任一命中即可（string | null 接受 None）。
        updates = Harness({"success": True, "data": None}).call(
            _validator_state({"type": ["string", "null"]})
        )
        assert updates["tool_results"][0]["success"] is True

    def test_malformed_contract_shapes_pass_through(self) -> None:
        # 契约值非 dict / schema 非 dict → 不校验，原样成功。
        for contracts in ({"bash": "junk"}, {"bash": {"render": "table"}}):
            updates = Harness({"success": True, "data": {}}).call(
                {"tool_output_contracts": contracts}
            )
            assert updates["tool_results"][0]["success"] is True

    def test_validation_switch_off(self) -> None:
        updates = Harness({"success": True, "data": "scalar"}).call(
            _validator_state({"type": "object"}, enabled="off")
        )
        assert updates["tool_results"][0]["success"] is True


# ── 错误信封串化 ────────────────────────────────────────────────


class TestErrorEnvelopeStringify:
    """信封 error 是字符串；事件载荷串化走 error_to_contract_string 三分支。"""

    def test_error_string_passthrough_in_event(self) -> None:
        h = Harness({"success": False, "error": "boom"})
        updates = h.call({})
        assert updates["tool_results"][0]["success"] is False
        assert _tool_event(h.events)["payload"]["error"] == "boom"

    def test_error_dict_message_extracted(self) -> None:
        assert _MOD.error_to_contract_string({"message": "boom", "code": "X"}) == "boom"

    def test_error_dict_without_message_json_fallback(self) -> None:
        out = _MOD.error_to_contract_string({"code": "X"})
        assert '"code"' in out and "X" in out

    def test_error_scalar_json_fallback(self) -> None:
        assert _MOD.error_to_contract_string(42) == "42"


# ── 多模态 ──────────────────────────────────────────────────────

_IMG_B64 = "aGVsbG8="


class TestMultimodal:
    def _result(self, data: Any, metadata: Any = None) -> dict[str, Any]:
        resp: dict[str, Any] = {"success": True, "data": data}
        if metadata is not None:
            resp["metadata"] = metadata
        return resp

    def test_three_sources_collected_into_event(self) -> None:
        data = {
            "base64_data": _IMG_B64,
            "mime_type": "image/png",
            "path": "/uploads/a.png",
            "images": [
                {"base64": _IMG_B64, "mime_type": "image/jpeg", "path": "/b.jpg"},
                {"mime_type": "image/gif"},  # 缺 base64 → 跳过
            ],
        }
        metadata = {
            "multimodal_content": [
                {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{_IMG_B64}"}},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},  # 非 data URL → 跳过
                {"type": "text", "text": "t"},  # 非图块 → 跳过
            ]
        }
        h = Harness(self._result(data, metadata))
        updates = h.call({"llm_supports_vision": False})
        mm_events = [e for e in h.events if e.get("event") == "tool_multimedia_result"]
        assert len(mm_events) == 1, "三来源去重后恰一事件（缺 base64/非 data URL 跳过）"
        multimedia = mm_events[0]["payload"]["multimedia"]
        assert len(multimedia) == 3
        assert multimedia[0] == {"mime_type": "image/png", "path": "/uploads/a.png"}
        assert updates["messages"] and updates["messages"]["_ops"]

    def test_vision_off_yields_guidance_message(self) -> None:
        h = Harness(self._result({"base64_data": _IMG_B64, "mime_type": "image/png", "path": ""}))
        updates = h.call({"llm_supports_vision": False})
        ops = updates["messages"]["_ops"]
        guidance = [op for op in ops if (op.get("msg") or {}).get("name") == "tool_images"]
        assert guidance and "analyze_image" in guidance[0]["msg"]["content"]

    def test_vision_on_yields_image_blocks(self) -> None:
        h = Harness(self._result({"base64_data": _IMG_B64, "mime_type": "image/png", "path": ""}))
        updates = h.call({"llm_supports_vision": True})
        ops = updates["messages"]["_ops"]
        guidance = [op for op in ops if (op.get("msg") or {}).get("name") == "tool_images"]
        assert guidance
        blocks = guidance[0]["msg"]["content"]
        assert any(
            isinstance(b, dict) and b.get("type") == "image_url" and _IMG_B64 in b["image_url"]["url"]
            for b in blocks
        ), "视觉开启 → data URL 图块注入"

    def test_no_images_no_multimodal_event(self) -> None:
        h = Harness({"success": True, "data": {"output": "plain"}})
        h.call({})
        assert not [e for e in h.events if e.get("event") == "tool_multimedia_result"]


# ── 消息 op ────────────────────────────────────────────────────


class TestMessageOps:
    def test_assistant_message_synthesized_when_missing(self) -> None:
        # 快照缺 assistant(tool_calls) 配对消息 → 补造一条在最前。
        h = Harness({"success": True, "data": {"output": "ok"}})
        updates = h.call({})
        ops = updates["messages"]["_ops"]
        assert ops[0]["msg"]["role"] == "assistant"
        assert ops[0]["msg"]["tool_calls"][0]["id"] == "c1"

    def test_output_truncated_note_variants(self) -> None:
        base = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}],
                    "seq": 0,
                }
            ],
        }
        # 带 lines：注记含行数。
        h = Harness(
            {"success": True, "data": {"output": "x", "lines": 3}, "metadata": None}
        )
        updates = h.call({**base, "output_truncated": True})
        tool_ops = [op for op in updates["messages"]["_ops"] if (op.get("msg") or {}).get("role") == "tool"]
        assert "已写入 3 行" in tool_ops[0]["msg"]["content"]
        # file_write：注记追加续写引导。
        state = {
            **base,
            "output_truncated": True,
            "current_call": {"name": "file_write", "id": "c1", "args": {}},
        }
        h2 = Harness({"success": True, "data": {"output": "x"}})
        updates2 = h2.call(state)
        tool_ops2 = [
            op for op in updates2["messages"]["_ops"] if (op.get("msg") or {}).get("role") == "tool"
        ]
        assert "file_write(action=append)" in tool_ops2[0]["msg"]["content"]


# ── 副作用 / 预览 / 兜底 ────────────────────────────────────────


class TestSideEffectsAndFallbacks:
    def test_task_evaluate_completed_evidence(self) -> None:
        state = {
            "current_call": {"name": "task_evaluate", "id": "c1", "args": {}},
        }
        h = Harness(
            {
                "success": True,
                "data": {"task_id": "t1"},
                "metadata": {"result": "completed"},
            }
        )
        updates = h.call(state)
        assert updates["task_evaluation_completed"] is True

    def test_human_interaction_conversation_signal_nested(self) -> None:
        for data in (
            {"conversation_mode": True},
            {"output": {"conversation_mode": True}},
        ):
            state = {"current_call": {"name": "human_interaction", "id": "c1", "args": {}}}
            h = Harness({"success": True, "data": data})
            updates = h.call(state)
            assert updates["_pending_route_signal"]["route_type"] == "wait"

    def test_raw_result_preview_success_and_failure(self) -> None:
        ok = Harness({"success": True, "data": "plain text"}).call({})
        # 非 object data → JSON 串化预览（对齐 Rust to_string：字符串带引号）。
        assert '"plain text"' in ok["raw_result"]
        bad = Harness({"success": False, "error": "boom"}).call({})
        assert "boom" in bad["raw_result"]

    def test_event_delegate_exception_does_not_block(self) -> None:
        h = Harness({"success": True, "data": {"output": "ok"}})

        async def broken_event(params: dict) -> None:
            raise RuntimeError("bus down")

        h._plugin.set_event_delegate(broken_event)
        updates = h.call({})
        assert updates["tool_results"][0]["success"] is True, "观测面失败不阻断执行"

    def test_tool_delegate_non_dict_response_fails(self) -> None:
        h = Harness("junk")
        updates = h.call({})
        assert updates["tool_results"][0]["success"] is False

    def test_pre_decided_by_name_fallback_with_extra_passthrough(self) -> None:
        # 无 call_id entry 按工具名兜底命中；七键外扩展位随结果透传。
        state = {
            "current_call": {"name": "bash", "id": "", "args": {}},
            "pre_decided_results": [
                {
                    "tool_name": "bash",
                    "success": False,
                    "error": "denied",
                    "retry_allowed": True,
                }
            ],
        }
        h = Harness({"success": True, "data": {}})
        updates = h.call(state)
        result = updates["tool_results"][0]
        assert result["success"] is False and result["error"] == "denied"
        assert result["retry_allowed"] is True, "扩展位随 tool_results 透传"
        assert not h.tool_params, "预填命中不得触达 tool-executor"


# ── 尾批：属性面 / 串化 str 分支 / 标量类型矩阵 / 多模态兜底 ──────────


def test_priority_property() -> None:
    """priority 属性面（管道步骤排序读它）。"""
    assert Harness()._plugin.priority == 50


def test_error_contract_string_str_passthrough() -> None:
    """str 直传分支（事件载荷最常见形态）。"""
    assert _MOD.error_to_contract_string("plain") == "plain"


class TestValidatorScalarTypes:
    """标量类型契约矩阵：命中与失配各一，覆盖 matches 分支与类型名映射。"""

    @pytest.mark.parametrize(
        ("schema_type", "good", "bad", "bad_type_name"),
        [
            ("boolean", True, "x", "string"),
            ("null", None, "x", "string"),
            ("number", 1.5, "x", "string"),
            ("string", "s", 1, "number"),
            ("array", [1], {}, "object"),
            ("object", {"a": 1}, [], "array"),
        ],
    )
    def test_scalar_contract(self, schema_type, good, bad, bad_type_name) -> None:
        h_ok = Harness({"success": True, "data": good})
        assert h_ok.call(_validator_state({"type": schema_type}))["tool_results"][0][
            "success"
        ] is True
        h_bad = Harness({"success": True, "data": bad})
        updates = h_bad.call(_validator_state({"type": schema_type}))
        result = updates["tool_results"][0]
        assert result["success"] is False
        assert bad_type_name in result["error"], "失败消息含实际类型名"

    def test_type_decl_non_string_non_list_unconstrained(self) -> None:
        """type 声明既非 str 也非 list（配置手滑写数字）→ 不约束。"""
        updates = Harness({"success": True, "data": 42}).call(_validator_state({"type": 123}))
        assert updates["tool_results"][0]["success"] is True

    def test_enum_bool_and_numeric_equality(self) -> None:
        # bool 与数值不混淆（True ∉ [1]）；数值跨 int/float 相等。
        h_bool = Harness({"success": True, "data": True})
        assert h_bool.call(_validator_state({"type": "boolean", "enum": [1]}))[
            "tool_results"
        ][0]["success"] is False
        h_num = Harness({"success": True, "data": 1.0})
        assert h_num.call(_validator_state({"type": "number", "enum": [1]}))[
            "tool_results"
        ][0]["success"] is True


class TestMultimodalFallbacks:
    def test_data_url_without_base64_skipped(self) -> None:
        data = {
            "images": [{"base64": _IMG_B64, "mime_type": "image/png"}],
        }
        metadata = {
            "multimodal_content": [
                {"type": "image_url", "image_url": {"url": "data:image/png"}},  # 无 ;base64, → 跳过
                {"type": "image_url", "junk": True},  # image_url 非 dict → 跳过
            ],
            "unrelated": 1,
        }
        h = Harness(
            {
                "success": True,
                "data": {**data, "base64_data": _IMG_B64, "mime_type": "image/png", "path": ""},
                "metadata": metadata,
            }
        )
        h.call({"llm_supports_vision": False})
        mm = [e for e in h.events if e.get("event") == "tool_multimedia_result"]
        assert len(mm) == 1
        assert len(mm[0]["payload"]["multimedia"]) == 2, "仅两个合法来源入事件"

    def test_no_event_delegate_images_still_injected(self) -> None:
        """event 委托缺省（None）→ 事件静默跳过，图块消息照常注入。"""
        h = Harness(
            {
                "success": True,
                "data": {"base64_data": _IMG_B64, "mime_type": "image/png", "path": ""},
            }
        )
        h._plugin.set_event_delegate(None)
        updates = h.call({"llm_supports_vision": False})
        ops = updates["messages"]["_ops"]
        assert [op for op in ops if (op.get("msg") or {}).get("name") == "tool_images"]
        assert not [e for e in h.events if e.get("event") == "tool_multimedia_result"]


class TestTailSlivers:
    def test_integer_rejects_bool(self) -> None:
        """integer 契约显式拒绝 bool（bool 是 int 子型，须排除）。"""
        updates = Harness({"success": True, "data": True}).call(
            _validator_state({"type": "integer"})
        )
        assert updates["tool_results"][0]["success"] is False

    @pytest.mark.parametrize(
        ("schema_type", "bad_data", "bad_type_name"),
        [
            ("string", None, "null"),
            ("object", True, "boolean"),
            ("object", ("t",), "unknown"),
        ],
    )
    def test_json_type_name_branches(self, schema_type, bad_data, bad_type_name) -> None:
        updates = Harness({"success": True, "data": bad_data}).call(
            _validator_state({"type": schema_type})
        )
        assert bad_type_name in updates["tool_results"][0]["error"]

    def test_multimodal_event_delegate_exception_does_not_block(self) -> None:
        """多模态事件委托抛异常 → 仅告警，图块消息照常注入。"""
        h = Harness(
            {
                "success": True,
                "data": {"base64_data": _IMG_B64, "mime_type": "image/png", "path": ""},
            }
        )

        async def broken_event(params: dict) -> None:
            raise RuntimeError("bus down")

        h._plugin.set_event_delegate(broken_event)
        updates = h.call({"llm_supports_vision": False})
        ops = updates["messages"]["_ops"]
        assert [op for op in ops if (op.get("msg") or {}).get("name") == "tool_images"]
