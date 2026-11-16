# @feature: FP-0.2.二 tool_schema_validator 解析失败不吞错 | @ci: python-coverage
"""tool_schema_validator 拦截契约 — 单元测试。

验证核心契约：缺 required 参数 / 无法自动修复的类型不匹配时，
调用被拦截（不进入 validated_calls，tool_core 不会执行），并注入一条
role=tool 诊断消息把缺失/类型错误明细反馈给 LLM，使其能补齐参数
而非盲目重试（与截断检测 _check_args_truncation 范式一致）。

回归场景：模型调用 file_write 时漏传 action，工具拿到 action=None
返回 "不支持的操作: None" 这一不透明错误，导致 LLM 无限重复调用。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("tool_schema_validator")

from typing import Any

import plugin as _tsv_plugin_mod  # noqa: E402  模块级捕获：测试内 re-import 会被裸名逐出干扰
import pytest
from _libs.pipeline_base.plugin import PluginContext
from _libs.pipeline_base.types import StateKeys

pytestmark = pytest.mark.unit


# 模拟 file_write 的 input_schema（required = ["action", "path"]）
_FILE_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["action", "path"],
}


class _FakeTool:
    """最小 Tool 替身：仅暴露 validator 实际使用的 name / input_schema。"""

    def __init__(self, name: str, input_schema: dict[str, Any]) -> None:
        self.name = name
        self.input_schema = input_schema


class _FakeRegistry:
    """最小 tool_registry 替身：list_all() 返回已知工具列表。"""

    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = {t.name: t for t in tools}

    def list_all(self) -> list[_FakeTool]:
        return list(self._tools.values())


_REGISTRY = _FakeRegistry([_FakeTool("file_write", _FILE_WRITE_SCHEMA)])


def _make_plugin() -> Any:
    add_plugin_dir("tool_schema_validator")
    from plugin import ToolSchemaValidator
    return ToolSchemaValidator()


def _make_ctx(
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]] | None = None,
    *,
    output_truncated: bool = False,
) -> PluginContext:
    state: dict[str, Any] = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: tool_calls,
        "messages": messages if messages is not None else [],
    }
    if output_truncated:
        state["output_truncated"] = True
    return PluginContext(
        state=state,
        _services={"tool_registry": _REGISTRY},
    )


def _pre_by_call_id(updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """pre_decided_results 按 call_id 索引（结果预填契约，ADR 2026-09-28）。"""
    return {e.get("call_id"): e for e in updates.get("pre_decided_results", [])}


def _pre_by_name(updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """无 call_id 条目按工具名索引（名字兜底）。"""
    return {
        e.get("tool_name"): e
        for e in updates.get("pre_decided_results", [])
        if e.get("call_id") is None
    }


class TestSchemaValidationBlock:
    """缺 required / 无法修复的类型不匹配 → 拦截 + 注入诊断。"""

    @pytest.mark.asyncio
    async def test_missing_required_blocked_and_feedback(self) -> None:
        """缺 action（required）→ 拦截，不执行，注入诊断消息含缺失字段名。

        这是用户报的循环 bug 的核心回归：模型漏传 action 时不再漏到
        工具层返回模糊的 "不支持的操作: None"。
        """
        plugin = _make_plugin()
        # 模型只传了 path（业务数据），漏了 action
        tc = {"name": "file_write", "args": {"path": "/tmp/x.txt"}, "id": "call_1"}
        ctx = _make_ctx([tc])

        result = await plugin.execute(ctx)
        updates = result.state_updates

        # 被拒调用保留在 raw_tool_calls（tool_core 按 call_id 命中跳过执行）
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [tc["id"] for tc in remaining] == ["call_1"]

        # 预填拒绝结果（结果预填契约，ADR 2026-09-28）
        entry = _pre_by_call_id(updates)["call_1"]
        assert entry["success"] is False
        assert "SCHEMA_VALIDATION_FAILED" in entry["error"]
        assert "Missing required field: action" in entry["error"]
        assert entry["metadata"]["decided_by"] == "tool_schema_validator"

        # 不再自建配对消息（tool_core 统一整形）
        assert "messages" not in updates

        # 记录到 schema_errors 供观测
        assert updates.get("schema_errors")

    @pytest.mark.asyncio
    async def test_blocked_keeps_assistant_tool_call_sequence(self) -> None:
        """拦截后注入的 tool 消息紧跟在 assistant(tool_calls) 之后，序列完整。

        回归消息序列契约：assistant(tool_calls) → tool 必须成对，
        否则下一轮 LLM 报 "tool id not found"。
        """
        plugin = _make_plugin()
        tc = {"name": "file_write", "args": {"path": "/a"}, "id": "call_9"}
        messages_before = [
            {"role": "user", "content": "写入文件"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_9", "type": "function",
                    "function": {"name": "file_write"},
                }],
            },
        ]
        ctx = _make_ctx([tc], messages=list(messages_before))

        result = await plugin.execute(ctx)
        updates = result.state_updates

        # 配对完整性经结果预填达成：调用保留（tool_core 命中预定结果后统一
        # 整形 assistant(tool_calls) → tool 配对），validator 不动 messages。
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["call_9"]
        assert _pre_by_call_id(updates)["call_9"]["success"] is False
        assert "messages" not in updates

    @pytest.mark.asyncio
    async def test_unrepairable_type_mismatch_blocked(self) -> None:
        """类型不匹配且无法自动修复 → 拦截。

        action 期望 string，传入 dict（_try_convert 不处理 dict→string），
        修复仍失败 → 拦截。
        """
        plugin = _make_plugin()
        tc = {
            "name": "file_write",
            "args": {"action": {"oops": 1}, "path": "/a"},  # action 是 dict 而非 string
            "id": "call_2",
        }
        ctx = _make_ctx([tc])

        result = await plugin.execute(ctx)
        updates = result.state_updates

        # 被拒调用保留 + 预填拒绝结果
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["call_2"]
        assert _pre_by_call_id(updates)["call_2"]["success"] is False
        assert "Type mismatch for 'action'" in _pre_by_call_id(updates)["call_2"]["error"]


class TestSchemaValidationPass:
    """合法调用 / 可修复 → 放行。"""

    @pytest.mark.asyncio
    async def test_valid_call_passes(self) -> None:
        """参数齐全且类型正确 → 放行执行，不注入诊断消息。"""
        plugin = _make_plugin()
        tc = {
            "name": "file_write",
            "args": {"action": "write", "path": "/a", "content": "hi"},
            "id": "call_3",
        }
        ctx = _make_ctx([tc])

        result = await plugin.execute(ctx)
        updates = result.state_updates

        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(remaining) == 1
        assert remaining[0]["id"] == "call_3"
        # 不预填、不动 messages
        assert "pre_decided_results" not in updates
        assert "messages" not in updates

    @pytest.mark.asyncio
    async def test_repairable_type_mismatch_autofixed(self) -> None:
        """类型不匹配但可自动修复 → 放行，且 args 被修复。

        action 期望 string，传入 int 123 → _try_convert 转为 "123" → 放行。
        """
        plugin = _make_plugin()
        tc = {
            "name": "file_write",
            "args": {"action": 123, "path": "/a"},  # int → 自动转 string
            "id": "call_4",
        }
        ctx = _make_ctx([tc])

        result = await plugin.execute(ctx)
        updates = result.state_updates

        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(remaining) == 1
        # 归一契约：action 应为字符串 "123"
        assert remaining[0]["args"]["action"] == "123"


class TestSchemaValidationMixed:
    """混合调用：合法放行、非法拦截，互不影响。"""

    @pytest.mark.asyncio
    async def test_mixed_calls_selective_block(self) -> None:
        """一次产出的多个 tool_calls：合法的放行，缺参数的被拦截。

        验证拦截是逐调用的，不会因为一个坏调用把好的也丢了。
        """
        plugin = _make_plugin()
        good_tc = {
            "name": "file_write",
            "args": {"action": "write", "path": "/a"},
            "id": "good",
        }
        bad_tc = {
            "name": "file_write",
            "args": {"path": "/b"},  # 缺 action
            "id": "bad",
        }
        ctx = _make_ctx([good_tc, bad_tc])

        result = await plugin.execute(ctx)
        updates = result.state_updates

        # good 与 bad 都保留（拦截是逐调用的）；只有 bad 被预填拒绝
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [tc["id"] for tc in remaining] == ["good", "bad"]

        entries = _pre_by_call_id(updates)
        assert set(entries) == {"bad"}
        assert entries["bad"]["success"] is False
        assert "messages" not in updates


class TestTruncationHint:
    """output_truncated 信号 → 缺失字段的诊断消息带截断引导。"""

    @pytest.mark.asyncio
    async def test_truncated_missing_fields_hint_append(self) -> None:
        """输出被截断且缺必填字段 → 诊断消息提示分块/append 续写。

        回归：用户报 file_write 写大文件被截断，action 漏传后工具返回
        模糊的 "不支持的操作: None"。截断信号应让模型收到明确的续写指引。
        """
        plugin = _make_plugin()
        tc = {"name": "file_write", "args": {"path": "/a"}, "id": "call_t"}
        ctx = _make_ctx([tc], output_truncated=True)

        result = await plugin.execute(ctx)
        updates = result.state_updates

        # 仍拦截（缺必填 action）：调用保留 + 预填拒绝带截断引导
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["call_t"]
        entry = _pre_by_call_id(updates)["call_t"]
        error = entry["error"]
        # 截断引导文案出现
        assert "max_tokens 被截断" in error
        assert "append" in error
        # 透传 output_truncated 标志（结构化 metadata，非旧 JSON 文本）
        assert entry["metadata"]["output_truncated"] is True

    @pytest.mark.asyncio
    async def test_no_truncation_signal_no_hint(self) -> None:
        """无截断信号时，缺失字段的诊断不含截断引导（避免误导）。"""
        plugin = _make_plugin()
        tc = {"name": "file_write", "args": {"path": "/a"}, "id": "call_n"}
        ctx = _make_ctx([tc], output_truncated=False)

        result = await plugin.execute(ctx)
        updates = result.state_updates
        entry = _pre_by_call_id(updates)["call_n"]
        assert entry["success"] is False
        assert "max_tokens 被截断" not in entry["error"]


class TestRegistrySourceFallback:
    """schema 来源：registry 优先，无 registry 时回退 state。"""

    @pytest.mark.asyncio
    async def test_reads_from_registry_service(self) -> None:
        """注入 registry 服务时，schema 来自 registry（生产真实路径）。"""
        plugin = _make_plugin()
        tc = {"name": "file_write", "args": {"path": "/a"}, "id": "c1"}
        # ctx 仅提供 registry 服务，不提供 state["_tool_definitions"]
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
        }
        ctx = PluginContext(state=state, _services={"tool_registry": _REGISTRY})

        result = await plugin.execute(ctx)
        # registry 里有 file_write 定义 → 能识别缺 action 并预填拒绝
        updates = result.state_updates
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]
        assert _pre_by_call_id(updates)["c1"]["success"] is False

    @pytest.mark.asyncio
    async def test_falls_back_to_state_definitions(self) -> None:
        """无 registry 服务时，回退 state["_tool_definitions"]（兼容测试夹具）。"""
        plugin = _make_plugin()
        tc = {"name": "file_write", "args": {"path": "/a"}, "id": "c2"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"file_write": {"input_schema": _FILE_WRITE_SCHEMA}},
        }
        ctx = PluginContext(state=state, _services={})  # 无 registry

        result = await plugin.execute(ctx)
        # 回退路径也能拦截缺 action（预填拒绝）
        updates = result.state_updates
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c2"]
        assert _pre_by_call_id(updates)["c2"]["success"] is False


class TestObjectArrayParseFailureKeepsOriginal:
    """P9（兜底反模式审查）：object/array 解析失败返回原值交给类型校验。

    旧缺陷：解析失败返回 {}/[] 是合法目标类型，会通过类型校验——
    坏参数静默变"合法空参"（与 integer 分支返回原值的语义不一致）。
    """

    _OPT_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "options": {"type": "object"},
            "tags": {"type": "array"},
        },
        "required": ["options"],
    }

    def _ctx_with(self, tc: dict[str, Any]) -> PluginContext:
        registry = _FakeRegistry([_FakeTool("options_tool", self._OPT_SCHEMA)])
        return PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [tc],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )

    @pytest.mark.asyncio
    async def test_invalid_object_string_blocked_as_type_error(self) -> None:
        """非 JSON 的 object 参数 → 保留原值 → 拦截 + 诊断（不再伪装空 dict）。"""
        plugin = _make_plugin()
        tc = {"name": "options_tool", "args": {"options": "not-json{"}, "id": "c_obj"}
        result = await plugin.execute(self._ctx_with(tc))
        updates = result.state_updates
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c_obj"], "调用保留（tool_core 幂等跳过）"
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["c_obj"]["error"]

    @pytest.mark.asyncio
    async def test_invalid_array_string_blocked_as_type_error(self) -> None:
        """非 JSON 的 array 参数 → 保留原值 → 拦截 + 诊断（不再伪装空 list）。"""
        plugin = _make_plugin()
        tc = {"name": "options_tool", "args": {"options": {}, "tags": "a, b"}, "id": "c_arr"}
        result = await plugin.execute(self._ctx_with(tc))
        updates = result.state_updates
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c_arr"], "调用保留"
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["c_arr"]["error"]

    @pytest.mark.asyncio
    async def test_valid_stringified_object_still_converted(self) -> None:
        """合法 JSON 字符串 → 照常转换放行（回归：修复意图不受影响）。"""
        plugin = _make_plugin()
        tc = {
            "name": "options_tool",
            "args": {"options": '{"a": 1}', "tags": "[1, 2]"},
            "id": "c_ok",
        }
        result = await plugin.execute(self._ctx_with(tc))
        calls = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(calls) == 1, "合法字符串化 JSON 应转换后放行"
        assert calls[0]["args"]["options"] == {"a": 1}
        assert calls[0]["args"]["tags"] == [1, 2]


# ─────────────────── JSON 修复能力通道（llm.repair_json 收敛点）───────────────────


class _StubRepairCaller:
    """可编程能力调用替身：模拟 tool-executor 传输与信封形态。"""

    def __init__(
        self,
        *,
        envelope: Any = None,
        exc: Exception | None = None,
        repaired: Any = '{"goal": "x"}',
    ) -> None:
        self._envelope = envelope
        self._exc = exc
        self._repaired = repaired

    async def __call__(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:  # noqa: ARG002
        if self._exc is not None:
            raise self._exc
        if self._envelope is not None:
            return self._envelope
        return {"success": True, "data": {"repaired": self._repaired}, "error": None}


class TestRepairCapabilityChannel:
    """_repair_json_string 经 llm.repair_json 能力：降级分支恒返回 None。"""

    @pytest.mark.asyncio
    async def test_no_caller_returns_none(self, monkeypatch: Any) -> None:
        """能力调用器未注入 → None（截断检测按不可修复降级）。"""
        p = _make_plugin()
        monkeypatch.setitem(
            p._check_args_truncation.__globals__, "_capability_caller", None
        )
        assert await _tsv_plugin_mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_caller_exception_returns_none(self, monkeypatch: Any) -> None:
        """能力调用失败（通道故障）→ None，不向验证主流程传播。"""
        p = _make_plugin()
        monkeypatch.setitem(
            p._check_args_truncation.__globals__,
            "_capability_caller",
            _StubRepairCaller(exc=RuntimeError("bus down")),
        )
        assert await _tsv_plugin_mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_failure_envelope_returns_none(self, monkeypatch: Any) -> None:
        """success=false 信封 → None。"""
        p = _make_plugin()
        monkeypatch.setitem(
            p._check_args_truncation.__globals__,
            "_capability_caller",
            _StubRepairCaller(envelope={"success": False, "data": None, "error": "down"}),
        )
        assert await _tsv_plugin_mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_truncation_detected_via_capability(self, monkeypatch: Any) -> None:
        """真实通道形态：桩替身返回修复结果 → 截断被识别并报告丢失字段。"""
        p = _make_plugin()
        monkeypatch.setitem(
            p._check_args_truncation.__globals__, "_capability_caller", _StubRepairCaller()
        )
        result = await p._check_args_truncation('{"goal": "x", "steps": ["a",', "my_tool")
        assert result is not None
        assert result["truncated"] is True
        assert "steps" in result["lost_keys"]


# ─────────────────── 缺口补测（2026-09-13 官方车道 miss 48→0）───────────────────


class _FreshModule:
    """同一 plugin 模块对象上操作的工具集。

    add_plugin_dir 每次逐出裸名 "plugin" → _make_plugin() 产生新模块对象；
    能力调用器注入（set_capability_caller 写模块全局）必须与被测插件/修复函数
    同源，否则全局写进旧对象、新对象读到 None（历史缺口根因）。
    """

    def __init__(self) -> None:
        add_plugin_dir("tool_schema_validator")
        import plugin as mod

        self._mod = mod

    @property
    def mod(self) -> Any:
        return self._mod

    def plugin(self, config: dict[str, Any] | None = None) -> Any:
        return self._mod.ToolSchemaValidator(config)


@pytest.fixture
def tsv() -> _FreshModule:
    fm = _FreshModule()
    yield fm
    fm.mod.set_capability_caller(None)  # 清全局，防跨测试泄漏


def _state_ctx(state: dict[str, Any]) -> PluginContext:
    base: dict[str, Any] = {
        StateKeys.CORE_TYPE: "tool_execute",
        StateKeys.RAW_TOOL_CALLS: [],
        "messages": [],
    }
    base.update(state)
    return PluginContext(state=base, _services={})


class _BoomRegistry:
    """list_all() 必炸的 registry 替身：驱动 registry 异常回退分支。"""

    def list_all(self) -> list[Any]:
        raise RuntimeError("registry down")


class TestPluginBasics:
    """启用开关 / 空输入短路 / 优先级配置。"""

    @pytest.mark.asyncio
    async def test_disabled_plugin_is_noop(self) -> None:
        """enabled=False → 完全不出面：非法调用也不拦截、无任何 state 更新。"""
        fm = _FreshModule()
        plugin = fm.plugin({"enabled": False})
        tc = {"name": "file_write", "args": {"path": "/a"}, "id": "c1"}  # 缺 action

        result = await plugin.execute(_state_ctx({StateKeys.RAW_TOOL_CALLS: [tc]}))

        assert result.state_updates == {}
        assert "schema_errors" not in result.state_updates

    @pytest.mark.asyncio
    async def test_no_tool_calls_is_noop(self) -> None:
        """无工具调用 → 直接空结果，不产出 schema_validated 等键。"""
        fm = _FreshModule()
        result = await fm.plugin().execute(_state_ctx({}))

        assert result.state_updates == {}

    def test_priority_from_config_with_default(self) -> None:
        """priority 可配置，缺省 30（校验级：参数注入之后）。"""
        fm = _FreshModule()
        assert fm.plugin().priority == 30
        assert fm.plugin({"priority": 7}).priority == 7


class TestRegistryFailureFallback:
    """registry.list_all() 异常 → 回退 state['_tool_definitions']，不炸管道。"""

    @pytest.mark.asyncio
    async def test_falls_back_to_state_when_registry_raises(self) -> None:
        plugin = _make_plugin()
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [{"name": "file_write", "args": {"path": "/a"}, "id": "c1"}],
            "messages": [],
            "_tool_definitions": {"file_write": {"input_schema": _FILE_WRITE_SCHEMA}},
        }
        ctx = PluginContext(state=state, _services={"tool_registry": _BoomRegistry()})

        result = await plugin.execute(ctx)
        updates = result.state_updates

        # state 回退定义生效：缺 action 照常拦截（预填拒绝）
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]
        assert _pre_by_call_id(updates)["c1"]["success"] is False
        assert updates.get("schema_errors")

    @pytest.mark.asyncio
    async def test_registry_raise_without_state_defs_passes_through(self) -> None:
        """registry 炸且无 state 定义 → 无 schema 可查，非严格模式放行。"""
        plugin = _make_plugin()
        tc = {"name": "mystery_tool", "args": {"x": 1}, "id": "c2"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
        }
        ctx = PluginContext(state=state, _services={"tool_registry": _BoomRegistry()})

        result = await plugin.execute(ctx)

        assert [t["id"] for t in result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c2"]
        assert "schema_errors" not in result.state_updates


class TestUnknownToolHandling:
    """未知工具：非严格放行 / 严格拦截；定义缺 input_schema 视作无约束。"""

    @pytest.mark.asyncio
    async def test_unknown_tool_passes_in_lenient_mode(self) -> None:
        plugin = _make_plugin()
        tc = {"name": "mystery_tool", "args": {"x": 1}, "id": "c1"}

        result = await plugin.execute(_state_ctx({StateKeys.RAW_TOOL_CALLS: [tc]}))

        assert [t["id"] for t in result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]
        assert "schema_errors" not in result.state_updates

    @pytest.mark.asyncio
    async def test_unknown_tool_blocked_in_strict_mode(self) -> None:
        fm = _FreshModule()
        plugin = fm.plugin({"strict": True})
        tc = {"name": "mystery_tool", "args": {"x": 1}, "id": "c1"}

        result = await plugin.execute(_state_ctx({StateKeys.RAW_TOOL_CALLS: [tc]}))
        updates = result.state_updates

        # 调用保留（配对完整）+ 预填拒绝（LLM 收到反馈，不再静默丢调用）
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]
        entry = _pre_by_call_id(updates)["c1"]
        assert entry["success"] is False
        assert "未注册" in entry["error"]
        assert entry["metadata"]["error_code"] == "TOOL_NOT_FOUND"
        errors = updates.get("schema_errors", [])
        assert len(errors) == 1
        assert errors[0]["tool"] == "mystery_tool"
        assert "Tool definition not found: mystery_tool" in errors[0]["error"]

    @pytest.mark.asyncio
    async def test_definition_without_input_schema_is_unconstrained(self) -> None:
        """工具定义存在但无 input_schema → 无从校验，放行。"""
        plugin = _make_plugin()
        tc = {"name": "file_write", "args": {"anything": 1}, "id": "c1"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"file_write": {"description": "schema 缺失的定义"}},
        }

        result = await plugin.execute(_state_ctx(state))

        assert [t["id"] for t in result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]


class TestNumericTypeConversion:
    """string→integer / string→number：可解析修复放行，不可解析保原值拦截。"""

    @pytest.mark.parametrize(
        ("field", "raw", "converted"),
        [
            ("count", "42", 42),
            ("count", "-7", -7),  # 符号量级相反输入
            ("ratio", "3.5", 3.5),
            ("ratio", "1e3", 1000.0),
        ],
    )
    @pytest.mark.asyncio
    async def test_numeric_string_converted_and_passed(
        self, field: str, raw: str, converted: Any,
    ) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {field: {"type": "integer" if field == "count" else "number"}},
            "required": [field],
        }
        plugin = _make_plugin()
        tc = {"name": "calc", "args": {field: raw}, "id": "c1"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"calc": {"input_schema": schema}},
        }

        result = await plugin.execute(_state_ctx(state))

        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(remaining) == 1
        got = remaining[0]["args"][field]
        assert got == converted
        # 修复后类型必须真正落到目标类型（int/float），而非仅值相等
        assert isinstance(got, bool) is False
        assert isinstance(got, int if field == "count" else float)
        assert result.state_updates.get("schema_fixes")

    @pytest.mark.parametrize(
        ("field", "raw"),
        [("count", "abc"), ("ratio", "fast")],
    )
    @pytest.mark.asyncio
    async def test_unparseable_numeric_string_blocked(
        self, field: str, raw: str,
    ) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {field: {"type": "integer" if field == "count" else "number"}},
            "required": [field],
        }
        plugin = _make_plugin()
        tc = {"name": "calc", "args": {field: raw}, "id": "c1"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"calc": {"input_schema": schema}},
        }

        result = await plugin.execute(_state_ctx(state))
        updates = result.state_updates

        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]
        entry = _pre_by_call_id(updates)["c1"]
        assert entry["success"] is False
        assert f"Type mismatch for '{field}'" in entry["error"]


class TestBooleanTypeConversion:
    """string→boolean：true/1→True、false/0→False，其余保原值拦截。"""

    _BOOL_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {"flag": {"type": "boolean"}},
        "required": ["flag"],
    }

    def _run(self, flag_value: Any) -> Any:
        plugin = _make_plugin()
        tc = {"name": "toggle", "args": {"flag": flag_value}, "id": "c1"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"toggle": {"input_schema": self._BOOL_SCHEMA}},
        }
        return plugin.execute(_state_ctx(state))

    @pytest.mark.parametrize(
        ("raw", "converted"),
        [("true", True), ("1", True), ("false", False), ("0", False)],
    )
    @pytest.mark.asyncio
    async def test_boolean_string_converted(self, raw: str, converted: bool) -> None:
        result = await self._run(raw)
        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])

        assert len(remaining) == 1
        got = remaining[0]["args"]["flag"]
        assert got is converted  # 必须是真 bool，而非 1/0
        assert isinstance(got, bool)

    @pytest.mark.asyncio
    async def test_unrecognized_boolean_string_blocked(self) -> None:
        result = await self._run("yes")

        updates = result.state_updates
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]
        assert "Type mismatch for 'flag'" in _pre_by_call_id(updates)["c1"]["error"]

    @pytest.mark.asyncio
    async def test_hostile_string_subclass_caught_by_conversion_guard(self) -> None:
        """值是行为异常的 str 子类（.lower() 抛错）→ 转换守卫兜底保原值，不外溢。"""

        class HostileStr(str):
            def lower(self) -> str:
                raise RuntimeError("hostile lower")

        result = await self._run(HostileStr("true"))

        updates = result.state_updates
        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c1"]
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["c1"]["error"]


class TestSchemaEdgeCases:
    """无 type 属性 / 未声明字段 / 未知名类型 → 不误伤。"""

    @pytest.mark.asyncio
    async def test_property_without_type_declaration_untouched(self) -> None:
        """字段声明无 type → 修复循环跳过（不转换也不报错），原值透传。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "memo": {},  # 无 type
            },
            "required": ["action"],
        }
        plugin = _make_plugin()
        memo_value = {"k": 1}
        # action 为 int → 首次校验报类型错，进入修复循环；memo 无 type 声明被跳过
        tc = {"name": "t", "args": {"action": 123, "memo": memo_value}, "id": "c1"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"t": {"input_schema": schema}},
        }

        result = await plugin.execute(_state_ctx(state))

        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert len(remaining) == 1
        assert remaining[0]["args"]["action"] == "123"  # 可修复字段照常修复
        assert remaining[0]["args"]["memo"] == memo_value  # 无 type 字段原值透传
        fixes = result.state_updates.get("schema_fixes", [])
        assert fixes, "修复有记录"
        assert all("memo" not in f for fix in fixes for f in fix["fixes"]), "无 type 字段不进修复"

    @pytest.mark.asyncio
    async def test_undeclared_extra_field_ignored(self) -> None:
        """args 携带 schema 未声明字段 → 不报类型错，照常放行。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        }
        plugin = _make_plugin()
        tc = {"name": "t", "args": {"action": "write", "extra": {"deep": [1, 2]}}, "id": "c1"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"t": {"input_schema": schema}},
        }

        result = await plugin.execute(_state_ctx(state))

        assert len(result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])) == 1
        assert "schema_errors" not in result.state_updates

    @pytest.mark.parametrize("weird_value", [None, "s", 1, [1]])
    @pytest.mark.asyncio
    async def test_unknown_schema_type_accepts_any_value(self, weird_value: Any) -> None:
        """未知名类型（不在类型表内）→ 恒视为匹配，任意值放行。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"w": {"type": "nulldata"}},
            "required": ["w"],
        }
        plugin = _make_plugin()
        tc = {"name": "t", "args": {"w": weird_value}, "id": "c1"}
        state = {
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [tc],
            "messages": [],
            "_tool_definitions": {"t": {"input_schema": schema}},
        }

        result = await plugin.execute(_state_ctx(state))

        assert len(result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])) == 1


class TestTruncationBlockingInExecute:
    """execute 阶段截断检测：注入 ARGS_TRUNCATED 诊断并拦下该调用。"""

    _GOOD_REPAIR = '{"path": "/a"}'  # 修复后丢 goal

    @pytest.mark.asyncio
    async def test_truncated_call_blocked_with_precise_diagnostic(self, tsv: _FreshModule) -> None:
        """截断调用 → 不进 validated（tool_core 不执行），注入精确诊断 tool 消息。"""
        tsv.mod.set_capability_caller(_StubRepairCaller(repaired=self._GOOD_REPAIR))
        tc = {"name": "file_write", "args": '{"path": "/a", "goal": "long text', "id": "c_t"}

        result = await tsv.plugin().execute(_state_ctx({StateKeys.RAW_TOOL_CALLS: [tc]}))
        updates = result.state_updates

        assert [tc["id"] for tc in updates.get(StateKeys.RAW_TOOL_CALLS, [])] == ["c_t"]
        assert "schema_errors" not in updates  # 截断不是 schema 错误，走独立诊断
        entry = _pre_by_call_id(updates)["c_t"]
        assert entry["success"] is False
        assert entry["metadata"]["error_code"] == "ARGS_TRUNCATED"
        assert entry["metadata"]["lost_keys"] == ["goal"]
        assert "goal" in entry["error"]

    @pytest.mark.asyncio
    async def test_truncation_skips_only_the_truncated_call(self, tsv: _FreshModule) -> None:
        """同一批混合调用：截断的拦下，完好的照常放行。"""
        tsv.mod.set_capability_caller(_StubRepairCaller(repaired=self._GOOD_REPAIR))
        bad = {"name": "file_write", "args": '{"path": "/a", "goal":', "id": "c_bad"}
        good = {"name": "file_write", "args": {"action": "write", "path": "/b"}, "id": "c_good"}

        result = await tsv.plugin().execute(
            _state_ctx({StateKeys.RAW_TOOL_CALLS: [bad, good]})
        )

        updates = result.state_updates
        # 两个调用都保留：截断的预填拒绝、完好的放行
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [tc["id"] for tc in remaining] == ["c_bad", "c_good"]
        assert _pre_by_call_id(updates)["c_bad"]["success"] is False


class TestRepairChannelDegradation:
    """能力通道各种异常形态 → 修复降级为不可修复（None），不炸验证主流程。"""

    @pytest.mark.asyncio
    async def test_caller_exception_degrades(self, tsv: _FreshModule) -> None:
        tsv.mod.set_capability_caller(_StubRepairCaller(exc=RuntimeError("bus down")))
        assert await tsv.mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_failure_envelope_degrades(self, tsv: _FreshModule) -> None:
        tsv.mod.set_capability_caller(
            _StubRepairCaller(envelope={"success": False, "data": None, "error": "down"})
        )
        assert await tsv.mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_non_dict_envelope_degrades(self, tsv: _FreshModule) -> None:
        tsv.mod.set_capability_caller(_StubRepairCaller(envelope="garbage"))
        assert await tsv.mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_non_dict_data_degrades(self, tsv: _FreshModule) -> None:
        tsv.mod.set_capability_caller(
            _StubRepairCaller(envelope={"success": True, "data": "not-a-dict", "error": None})
        )
        assert await tsv.mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_repaired_non_string_degrades(self, tsv: _FreshModule) -> None:
        tsv.mod.set_capability_caller(_StubRepairCaller(repaired=123))
        assert await tsv.mod._repair_json_string('{"goal": "x"') is None

    @pytest.mark.asyncio
    async def test_set_then_clear_caller(self, tsv: _FreshModule) -> None:
        """set_capability_caller 注入/清空往返：清空后修复不可用。"""
        tsv.mod.set_capability_caller(_StubRepairCaller())
        tsv.mod.set_capability_caller(None)
        assert await tsv.mod._repair_json_string('{"goal": "x"') is None


class TestTruncationDetectionBoundaries:
    """_check_args_truncation 判定边界：非截断形态一律返回 None。"""

    def _plugin_with_repair(self, tsv: _FreshModule, repaired: Any) -> Any:
        tsv.mod.set_capability_caller(_StubRepairCaller(repaired=repaired))
        return tsv.plugin()

    @pytest.mark.asyncio
    async def test_parseable_json_string_not_truncated(self) -> None:
        """args 是合法 JSON 字符串 → 直接解析成功，与截断无关。"""
        fm = _FreshModule()
        p = fm.plugin()
        assert await p._check_args_truncation('{"goal": "x"}', "t") is None

    @pytest.mark.asyncio
    async def test_no_caller_means_not_truncated(self, tsv: _FreshModule) -> None:
        """能力通道未注入 → 无法修复 → 不判截断（交给 tool_core 处理）。"""
        tsv.mod.set_capability_caller(None)
        assert await tsv.plugin()._check_args_truncation('{"goal": "x"', "t") is None

    @pytest.mark.asyncio
    async def test_unrepairable_result_not_truncated(self, tsv: _FreshModule) -> None:
        assert await self._plugin_with_repair(tsv, "not json{")._check_args_truncation(
            '{"goal": "x"', "t"
        ) is None

    @pytest.mark.asyncio
    async def test_non_dict_repaired_result_not_truncated(self, tsv: _FreshModule) -> None:
        """修复产物是数组 → 非 object 形态，不判截断。"""
        assert await self._plugin_with_repair(tsv, "[1, 2]")._check_args_truncation(
            '{"goal": "x"', "t"
        ) is None

    @pytest.mark.asyncio
    async def test_no_lost_keys_not_truncated(self, tsv: _FreshModule) -> None:
        """修复只是补全括号、无字段丢失 → 正常补全，不判截断。"""
        assert await self._plugin_with_repair(tsv, '{"goal": "x"}')._check_args_truncation(
            '{"goal": "x"', "t"
        ) is None

    @pytest.mark.asyncio
    async def test_unrepairable_args_pass_through_to_downstream(self) -> None:
        """端到端：无法修复的字符串 args 原样放行（交 tool_core 报精确错）。"""
        fm = _FreshModule()
        fm.mod.set_capability_caller(None)
        tc = {"name": "mystery_tool", "args": '{"goal": "x"', "id": "c1"}

        result = await fm.plugin().execute(_state_ctx({StateKeys.RAW_TOOL_CALLS: [tc]}))

        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [t["id"] for t in remaining] == ["c1"]
        assert remaining[0]["args"] == '{"goal": "x"'  # 原样透传，不猜测内容


# ─────────────── 嵌套属性校验 + array 包装 dict 解包（2026-10-16）───────────────


class TestNestedPropertyTraversal:
    """object 型 property 带 properties 声明 → 下钻校验/修复；array 期望收到
    单键包装 dict（{"item": v}/{"items": [...]}）→ 解包收编。

    背景（task_submit 二连败复盘）：模型把单文件列表发射成
    {"files": {"item": "docs/plugin_layering_plan.md"}}——顶层
    acceptance_criteria 是自由 object，顶层校验看不见内层类型错，
    坏形态漏到工具层被 fail-closed 拒绝，flash 档模型盲改二连败。
    """

    _NESTED_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "acceptance_criteria": {
                "type": "object",
                "properties": {
                    "files": {"type": "array"},
                    "expect": {"type": "string"},
                },
                "required": ["files"],
            },
        },
    }

    def _ctx_with(self, tc: dict[str, Any]) -> PluginContext:
        registry = _FakeRegistry([_FakeTool("submit_like", self._NESTED_SCHEMA)])
        return PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [tc],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )

    @pytest.mark.asyncio
    async def test_nested_scalar_wrapper_dict_unwrapped_and_passed(self) -> None:
        """嵌套 array 字段收到 {"item": 路径} → 解包为 [路径] 放行 + 修复留痕。"""
        plugin = _make_plugin()
        tc = {
            "name": "submit_like",
            "args": {
                "acceptance_criteria": {
                    "files": {"item": "docs/plugin_layering_plan.md"},
                    "expect": "含现状清单",
                }
            },
            "id": "call_n1",
        }
        result = await plugin.execute(self._ctx_with(tc))
        updates = result.state_updates

        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [t["id"] for t in remaining] == ["call_n1"], "解包后放行"
        assert remaining[0]["args"]["acceptance_criteria"]["files"] == [
            "docs/plugin_layering_plan.md"
        ]
        assert "pre_decided_results" not in updates, "不拦截"
        fixes = updates.get("schema_fixes")
        assert fixes, "修复必须留痕（schema_fixes）"
        assert "acceptance_criteria.files" in str(fixes)

    @pytest.mark.asyncio
    async def test_nested_list_valued_wrapper_unwrapped(self) -> None:
        """{"items": [a, b]} → [a, b]：元素保序不丢。"""
        plugin = _make_plugin()
        tc = {
            "name": "submit_like",
            "args": {"acceptance_criteria": {"files": {"items": ["a.md", "b.md"]}}},
            "id": "call_n2",
        }
        result = await plugin.execute(self._ctx_with(tc))
        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert remaining[0]["args"]["acceptance_criteria"]["files"] == ["a.md", "b.md"]

    @pytest.mark.asyncio
    async def test_nested_missing_required_reports_qualified_path(self) -> None:
        """嵌套 required 缺失 → 诊断带限定路径（内层字段可定位）。"""
        plugin = _make_plugin()
        tc = {
            "name": "submit_like",
            "args": {"acceptance_criteria": {"expect": "x"}},
            "id": "call_n3",
        }
        result = await plugin.execute(self._ctx_with(tc))
        entry = _pre_by_call_id(result.state_updates)["call_n3"]
        assert entry["success"] is False
        assert "Missing required field: acceptance_criteria.files" in entry["error"]

    @pytest.mark.asyncio
    async def test_nested_ambiguous_wrapper_still_blocked(self) -> None:
        """多键包装 dict（意图不明）→ 不猜测，仍拦截且错误带限定路径。"""
        plugin = _make_plugin()
        tc = {
            "name": "submit_like",
            "args": {"acceptance_criteria": {"files": {"a": "x.md", "b": "y.md"}}},
            "id": "call_n4",
        }
        result = await plugin.execute(self._ctx_with(tc))
        entry = _pre_by_call_id(result.state_updates)["call_n4"]
        assert entry["success"] is False
        assert "SCHEMA_VALIDATION_FAILED" in entry["error"]
        assert "acceptance_criteria.files" in entry["error"]

    @pytest.mark.asyncio
    async def test_free_form_object_without_properties_untouched(self) -> None:
        """无 properties 的自由 object（如指标名 dict）不下钻、零误伤。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"acceptance_criteria": {"type": "object"}},
        }
        registry = _FakeRegistry([_FakeTool("free_form", schema)])
        plugin = _make_plugin()
        tc = {
            "name": "free_form",
            "args": {
                "acceptance_criteria": {
                    "file_check": {"input_params": {"path": "x.md"}},
                    "anything": {"nested": [1, 2]},
                }
            },
            "id": "call_n5",
        }
        ctx = PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [tc],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )
        result = await plugin.execute(ctx)
        updates = result.state_updates
        assert "pre_decided_results" not in updates, "自由 object 不受下钻影响"
        assert "schema_fixes" not in updates

    @pytest.mark.asyncio
    async def test_top_level_array_wrapper_dict_unwrapped(self) -> None:
        """顶层 array 字段同样享受包装解包（通用能力，不限于嵌套层）。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"tags": {"type": "array"}},
        }
        registry = _FakeRegistry([_FakeTool("tags_tool", schema)])
        plugin = _make_plugin()
        tc = {"name": "tags_tool", "args": {"tags": {"item": "x"}}, "id": "call_n6"}
        ctx = PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [tc],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )
        result = await plugin.execute(ctx)
        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert remaining[0]["args"]["tags"] == ["x"]


# ─────────────── 常见形态目录兼容（2026-10-16 第二轮，业界实证）───────────────


class TestCommonShapeRepair:
    """LLM 工具调用常见形态走样目录（搜索实证：数字键对象化数组/围栏包裹/
    prose 夹带/单元素数组包对象/布尔与浮点的类型漂移）。无歧义形态收编，
    歧义形态不猜测（P9 反伪造立场不变：非 JSON 裸字符串仍拦截）。
    """

    _SHAPE_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "tags": {"type": "array"},
            "options": {"type": "object"},
            "count": {"type": "integer"},
            "content": {"type": "string"},
        },
    }

    def _ctx_with(self, tc: dict[str, Any]) -> PluginContext:
        registry = _FakeRegistry([_FakeTool("shape_tool", self._SHAPE_SCHEMA)])
        return PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [tc],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )

    async def _repaired_arg(self, args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        plugin = _make_plugin()
        tc = {"name": "shape_tool", "args": args, "id": "call_s"}
        result = await plugin.execute(self._ctx_with(tc))
        updates = result.state_updates
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        got = remaining[0]["args"] if remaining else None
        return got, updates

    @pytest.mark.asyncio
    async def test_numeric_indexed_dict_to_array(self) -> None:
        """{"0": a, "1": b} 数字键顺序对象 → [a, b]（业界实证高频形态）。"""
        got, _ = await self._repaired_arg({"tags": {"0": "x", "1": "y"}})
        assert got["tags"] == ["x", "y"]

    @pytest.mark.asyncio
    async def test_numeric_indexed_gap_dict_blocked(self) -> None:
        """数字键有空洞（0,2 缺 1）→ 序列化残缺不猜测，拦截。"""
        _, updates = await self._repaired_arg({"tags": {"0": "x", "2": "y"}})
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["call_s"]["error"]

    @pytest.mark.asyncio
    async def test_empty_dict_for_array_still_blocked(self) -> None:
        """空 dict 不解包为空数组（P9：不伪造合法空集合）。"""
        _, updates = await self._repaired_arg({"tags": {}})
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["call_s"]["error"]

    @pytest.mark.parametrize("scalar", [42, 3.5, True])
    @pytest.mark.asyncio
    async def test_bare_scalar_for_array_wrapped(self, scalar: Any) -> None:
        """裸标量（数字/布尔，无隐藏结构可言）→ 单元素数组。"""
        got, _ = await self._repaired_arg({"tags": scalar})
        assert got["tags"] == [scalar]

    @pytest.mark.asyncio
    async def test_fenced_json_strings_converted(self) -> None:
        """markdown 围栏包裹的 JSON（array/object 两型）→ 剥围栏解析。"""
        got, _ = await self._repaired_arg(
            {"tags": "```json\n[1, 2]\n```", "options": "```json\n{\"a\": 1}\n```"}
        )
        assert got["tags"] == [1, 2]
        assert got["options"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_prose_wrapped_json_extracted(self) -> None:
        """prose 夹带 JSON（"Here is the JSON: {...}"）→ 截取最外层括号解析。"""
        got, _ = await self._repaired_arg(
            {"options": "Here is the JSON: {\"a\": 1} hope this helps"}
        )
        assert got["options"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_unextractable_prose_still_blocked(self) -> None:
        """prose 里根本没 JSON → 不伪造，拦截（P9 立场）。"""
        _, updates = await self._repaired_arg({"options": "absolutely no json here"})
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["call_s"]["error"]

    @pytest.mark.asyncio
    async def test_object_single_element_array_unwrapped(self) -> None:
        """object 字段收到单元素数组 [{...}] → 取该元素。"""
        got, _ = await self._repaired_arg({"options": [{"a": 1}]})
        assert got["options"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_object_multi_element_array_not_guessed(self) -> None:
        """多元素数组包 object → 取哪个不明，不猜测，拦截。"""
        _, updates = await self._repaired_arg({"options": [{"a": 1}, {"b": 2}]})
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["call_s"]["error"]

    @pytest.mark.asyncio
    async def test_bool_for_string_coerced_lowercase(self) -> None:
        """string 字段收布尔 → "true"/"false"（小写，bool 分支先于 int）。"""
        got, _ = await self._repaired_arg({"content": True})
        assert got["content"] == "true"

    @pytest.mark.asyncio
    async def test_decimal_forms_for_integer_coerced(self) -> None:
        """integer 字段收 "3.0"/3.0 → 3（字符串经 float 桥接，浮点整数值直转）。"""
        got, _ = await self._repaired_arg({"count": "3.0"})
        assert got["count"] == 3
        got, _ = await self._repaired_arg({"count": 3.0})
        assert got["count"] == 3

    @pytest.mark.asyncio
    async def test_non_integral_float_for_integer_blocked(self) -> None:
        """integer 字段收 3.5 → 不可无损，不猜测，拦截。"""
        _, updates = await self._repaired_arg({"count": 3.5})
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["call_s"]["error"]

    @pytest.mark.asyncio
    async def test_numeric_indexed_form_composes_with_nested_traversal(self) -> None:
        """数字键形态与嵌套下钻组合：内层 array 字段同样收编。"""
        registry = _FakeRegistry([
            _FakeTool(
                "submit_like",
                {
                    "type": "object",
                    "properties": {
                        "acceptance_criteria": {
                            "type": "object",
                            "properties": {"files": {"type": "array"}},
                        }
                    },
                },
            )
        ])
        plugin = _make_plugin()
        tc = {
            "name": "submit_like",
            "args": {"acceptance_criteria": {"files": {"0": "a.md", "1": "b.md"}}},
            "id": "call_sn",
        }
        ctx = PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [tc],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )
        result = await plugin.execute(ctx)
        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert remaining[0]["args"]["acceptance_criteria"]["files"] == ["a.md", "b.md"]


# ─────────────── 修复层与工具契约对齐（2026-10-16 第三轮）───────────────


class TestMapFieldAlignment:
    """map 形 object（声明 additionalProperties）与工具契约对齐。

    实证背景：headers/query/params 类字段工具侧契约是 dict[str, str]
    （HTTP 语义），模型高频替身形态是 name/value 键值对列表。此前泛化的
    「单元素数组→取元素」对单对场景会修坏成 {"name": X, "value": Y} 当
    映射用（静默语义污染）——map 字段只收编合形 pairs，其余列表不猜。
    """

    _ALIGNED_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "metrics": {"type": "object", "additionalProperties": True},
            "data": {"type": ["object", "array"]},
            "options": {"type": "object"},
        },
    }

    def _ctx_with(self, tc: dict[str, Any]) -> PluginContext:
        registry = _FakeRegistry([_FakeTool("aligned_tool", self._ALIGNED_SCHEMA)])
        return PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [tc],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )

    async def _repaired_arg(self, args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        plugin = _make_plugin()
        tc = {"name": "aligned_tool", "args": args, "id": "call_m"}
        result = await plugin.execute(self._ctx_with(tc))
        updates = result.state_updates
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        return (remaining[0]["args"] if remaining else None), updates

    @pytest.mark.asyncio
    async def test_pairs_list_multi_converted_to_map(self) -> None:
        """name/value 对列表（多对）→ dict，HTTP 语义对齐。"""
        got, _ = await self._repaired_arg(
            {"headers": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]}
        )
        assert got["headers"] == {"A": "1", "B": "2"}

    @pytest.mark.asyncio
    async def test_pairs_list_single_converted_not_misread(self) -> None:
        """单对列表 [{"name": X, "value": Y}] → {"X": Y}，而非误读为
        {"name": X, "value": Y} 两个 header（对齐回归核心用例）。"""
        got, _ = await self._repaired_arg({"headers": [{"name": "X", "value": "Y"}]})
        assert got["headers"] == {"X": "Y"}

    @pytest.mark.parametrize(
        "pair", [{"key": "score", "value": 0.4}, {"k": "score", "v": 0.4}]
    )
    @pytest.mark.asyncio
    async def test_pairs_convention_variants(self, pair: dict) -> None:
        """key/value、k/v 同约定收编（值型不受 string 限制，随声明面）。"""
        got, _ = await self._repaired_arg({"metrics": [pair]})
        assert got["metrics"] == {"score": 0.4}

    @pytest.mark.asyncio
    async def test_malformed_pairs_list_blocked_not_unwrapped(self) -> None:
        """单键元素（缺取值侧）不合对形 → 不落泛化解包，明确拦截。"""
        _, updates = await self._repaired_arg({"headers": [{"name": "A"}]})
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["call_m"]["error"]

    @pytest.mark.asyncio
    async def test_non_pairs_multi_blocked(self) -> None:
        """非对形多元素列表 → 拦截。"""
        _, updates = await self._repaired_arg(
            {"headers": [{"foo": 1}, {"bar": 2}]}
        )
        assert "SCHEMA_VALIDATION_FAILED" in _pre_by_call_id(updates)["call_m"]["error"]

    @pytest.mark.asyncio
    async def test_map_fenced_string_still_parsed(self) -> None:
        """map 字段的字符串形态（围栏 JSON）仍走泛化字符串解析。"""
        got, _ = await self._repaired_arg({"headers": "```json\n{\"A\": \"1\"}\n```"})
        assert got["headers"] == {"A": "1"}

    @pytest.mark.asyncio
    async def test_union_typed_body_passes_untouched(self) -> None:
        """union（["object","array"]）字段 = 任意 JSON 体：列表直通不修不拦。

        对齐 web_operate.data（json=data 直传，列表体合法）——既不被
        泛化解包修坏（[{"a":1}] 保持列表），也不被类型闸拦截。
        """
        got, updates = await self._repaired_arg({"data": [1, 2, 3]})
        assert got["data"] == [1, 2, 3]
        assert "pre_decided_results" not in updates
        got, _ = await self._repaired_arg({"data": [{"a": 1}]})
        assert got["data"] == [{"a": 1}], "单元素列表体不得被解包修坏"

    @pytest.mark.asyncio
    async def test_plain_object_unwrap_unharmed(self) -> None:
        """非 map 的裸 object 字段：单元素数组解包照常（既有行为不回归）。"""
        got, _ = await self._repaired_arg({"options": [{"a": 1}]})
        assert got["options"] == {"a": 1}


# ─────────── 未声明参数策略（2026-10-18：additionalProperties:false 生效）───────────


_ROOT = Path(__file__).resolve().parents[1]


def _injector_native_keys() -> set[str]:
    """AST 提取 ``tool_args_inject._NATIVE_INJECT_KEYS`` 字面量（注入面真值）。

    非字面量（动态构造）时抛 AssertionError——注入键域漂移必须显式同步，
    不得静默放行（该集合决定校验层是否会把系统注入键误判为未知键）。
    """
    source = (_ROOT / "plugins" / "shared" / "tool_args_inject" / "plugin.py").read_text(
        encoding="utf-8"
    )
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "_NATIVE_INJECT_KEYS" for t in node.targets):
            continue
        literal = ast.literal_eval(node.value.args[0])
        assert isinstance(literal, set), "_NATIVE_INJECT_KEYS 非集合字面量，注入域不可判定"
        assert literal, "_NATIVE_INJECT_KEYS 字面量为空，注入域不可判定"
        return {str(k) for k in literal}
    raise AssertionError(
        "tools_args_inject 未以字面量声明 _NATIVE_INJECT_KEYS："
        "tool_schema_validator._SYSTEM_INJECT_KEYS 需同步（未声明键策略的注入面豁免）"
    )


class TestUnknownKeyPolicy:
    """声明 ``additionalProperties: false`` 的对象收到未声明键 → fail-closed
    拒绝，错误文案同时回显未知键与该 schema 声明的合法键（模型可自纠）。

    背景（全量解析 439 次工具调用）：额外键 28 次被**静默忽略**、不报错——
    模型以为发对了，参数直接丢掉（"静默失效"）。此前校验只查 required/type，
    ``enum/const/pattern/min/max/additionalProperties`` 一律不查。

    边界（JSON Schema 语义，存量工具零行为变化）：未声明
    ``additionalProperties``（或声明 true / 子 schema）照旧放行。
    """

    _STRICT: dict[str, Any] = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "waitUntil": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    }

    # 未表态（缺省）——存量绝大多数工具的形态，行为必须一字不变。
    _LOOSE: dict[str, Any] = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }

    async def _run(self, schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        """给定 schema/args 跑一轮真实 validator，返回 state_updates。"""
        registry = _FakeRegistry([_FakeTool("probe_tool", schema)])
        plugin = _make_plugin()
        ctx = PluginContext(
            state={
                StateKeys.CORE_TYPE: "tool_execute",
                StateKeys.RAW_TOOL_CALLS: [{"name": "probe_tool", "args": args, "id": "call_u1"}],
                "messages": [],
            },
            _services={"tool_registry": registry},
        )
        return (await plugin.execute(ctx)).state_updates

    @pytest.mark.asyncio
    async def test_strict_unknown_key_blocked_with_unknown_and_allowed_echo(self) -> None:
        """未声明键 → 拒绝（不进执行），错误同时回显未知键与合法键列表。"""
        args = {"url": "https://example.com", "file_check": {"path": "x.md"}}
        updates = await self._run(self._STRICT, args)
        entry = _pre_by_call_id(updates)["call_u1"]

        assert entry["success"] is False, "静默放行 = 参数被丢，必须拒绝"
        assert "SCHEMA_VALIDATION_FAILED" in entry["error"]
        assert "file_check" in entry["error"], "未回显未知键名，模型无法自纠"
        assert "url" in entry["error"], "未回显合法键列表"
        assert "waitUntil" in entry["error"], "未回显合法键列表"
        assert entry["metadata"]["unknown_keys"] == ["file_check"]

        # 拒绝经结果预填直出：调用保留在 raw_tool_calls（tool_core 配对执行面）
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [t["id"] for t in remaining] == ["call_u1"]
        # 不得静默剥键凑绿——原始 args 一字不动
        assert remaining[0]["args"] == args

    @pytest.mark.asyncio
    async def test_multiple_unknown_keys_all_echoed(self) -> None:
        """多个未声明键（含 None 值）全部回显，顺序稳定（sorted）。"""
        updates = await self._run(
            self._STRICT,
            {
                "url": "https://example.com",
                "evaluation_metrics": [1, 2],
                "AC_template": None,
            },
        )
        entry = _pre_by_call_id(updates)["call_u1"]

        assert entry["success"] is False
        assert "AC_template" in entry["error"]
        assert "evaluation_metrics" in entry["error"]
        assert entry["metadata"]["unknown_keys"] == ["AC_template", "evaluation_metrics"]

    @pytest.mark.asyncio
    async def test_loose_schema_unknown_key_still_passes(self) -> None:
        """未声明 additionalProperties → 照旧放行（存量工具契约不变）。"""
        extra = {"url": "https://example.com", "file_check": {"path": "x.md"}}
        updates = await self._run(self._LOOSE, extra)

        assert "pre_decided_results" not in updates, "未表态的工具不得被新策略拦下"
        assert "schema_errors" not in updates
        assert updates[StateKeys.RAW_TOOL_CALLS][0]["args"] == extra

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "additional",
        [True, {"type": "string"}],
        ids=["additionalProperties-true", "additionalProperties-subschema"],
    )
    async def test_open_additional_properties_passes(self, additional: Any) -> None:
        """additionalProperties 为 true / 子 schema → 放行（只有 false 才是拒绝面）。"""
        schema = dict(self._STRICT, additionalProperties=additional)
        updates = await self._run(
            schema, {"url": "https://example.com", "anything": "goes"}
        )
        assert "pre_decided_results" not in updates

    @pytest.mark.asyncio
    async def test_strict_declared_keys_pass(self) -> None:
        """全声明键齐备 → 放行，零误伤（无修复留痕）。"""
        updates = await self._run(
            self._STRICT, {"url": "https://example.com", "waitUntil": "load"}
        )
        assert "pre_decided_results" not in updates
        assert "schema_fixes" not in updates

    @pytest.mark.asyncio
    async def test_strict_empty_properties_allows_only_system_keys(self) -> None:
        """properties 为空 + 严格（browser_snapshot 同款）：无参放行，任何键拒绝。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        assert "pre_decided_results" not in await self._run(schema, {})

        updates = await self._run(schema, {"level": "error"})
        entry = _pre_by_call_id(updates)["call_u1"]
        assert entry["success"] is False
        assert "level" in entry["error"]
        assert "allowed fields: none" in entry["error"], "空声明面须回显「无合法键」"

    @pytest.mark.asyncio
    async def test_system_injected_keys_are_not_unknown(self) -> None:
        """注入键豁免：管道序 tool_args_inject → validator，校验面看到的是
        注入后 args（timestamp/pipeline_id 无条件注入，session_id/workspace
        等按 state 有值才注入）——LLM 未发出的键不得据此拒绝，否则严格工具
        在生产全轮必死。"""
        injected = sorted(_injector_native_keys())
        args: dict[str, Any] = {"url": "https://example.com"}
        args.update(dict.fromkeys(injected, "v"))
        updates = await self._run(self._STRICT, args)

        assert "pre_decided_results" not in updates, (
            f"系统注入键被判为未知键：{injected}"
        )

    def test_system_key_set_covers_injector_declaration(self) -> None:
        """两边相碰（漂移守卫）：validator 的豁免集合 ⊇ 注入方声明的注入键集。

        注入方增键而未同步 → 本测试红（生产表现为严格工具整轮被误拒），
        不靠人工记忆同步。
        """
        exempt = set(_tsv_plugin_mod._SYSTEM_INJECT_KEYS)
        declared = _injector_native_keys()
        assert declared <= exempt, (
            f"注入方新增键未同步进 _SYSTEM_INJECT_KEYS: {sorted(declared - exempt)}"
        )

    @pytest.mark.asyncio
    async def test_nested_strict_object_unknown_key_blocked(self) -> None:
        """嵌套 object 声明严格 → 下钻拒绝，错误带限定路径（内层可定位）。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "options": {
                    "type": "object",
                    "properties": {"waitUntil": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        }
        updates = await self._run(
            schema, {"url": "u", "options": {"waitUntil": "load", "typo": 1}}
        )
        entry = _pre_by_call_id(updates)["call_u1"]

        assert entry["success"] is False
        assert "options.typo" in entry["error"], "内层未知键须带限定路径"
        assert "allowed fields: options.waitUntil" in entry["error"]
        assert entry["metadata"]["unknown_keys"] == ["options.typo"]

    @pytest.mark.asyncio
    async def test_nested_strict_empty_object_declaration_blocked(self) -> None:
        """嵌套 object 只声明 additionalProperties:false（无 properties）→ 下钻拒绝。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "options": {"type": "object", "additionalProperties": False},
            },
        }
        assert "pre_decided_results" not in await self._run(schema, {"options": {}})

        updates = await self._run(schema, {"options": {"a": 1}})
        entry = _pre_by_call_id(updates)["call_u1"]
        assert entry["success"] is False
        assert "options.a" in entry["error"]

    @pytest.mark.asyncio
    async def test_nested_loose_object_untouched(self) -> None:
        """嵌套 object 未表态 → 不下钻拒绝（自由形态零误伤，既有行为不变）。"""
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "metrics": {"type": "object", "properties": {"score": {"type": "number"}}},
            },
        }
        updates = await self._run(
            schema, {"metrics": {"score": 1.0, "自由指标名": {"nested": True}}}
        )
        assert "pre_decided_results" not in updates

    @pytest.mark.asyncio
    async def test_case_mismatched_key_is_unknown_and_reports_both_errors(self) -> None:
        """键名大小写敏感（JSON Schema 语义）：URL ≠ url → 未知键 + 缺必填并存。"""
        updates = await self._run(self._STRICT, {"URL": "https://example.com"})
        entry = _pre_by_call_id(updates)["call_u1"]

        assert entry["success"] is False
        assert "Missing required field: url" in entry["error"]
        assert "URL" in entry["error"]
        assert entry["metadata"]["unknown_keys"] == ["URL"]

    @pytest.mark.asyncio
    async def test_real_browser_manifest_schema_enforcement(self) -> None:
        """采纳面实证：真实 browser 六件的 manifest schema（非手抄副本）在本层生效。

        browser 六件是全仓唯一声明 ``additionalProperties: false`` 的工具面
        （实现侧 ``_build_arguments`` 按声明白名单透传、上游 @playwright/mcp
        契约即严格）——本刀的采纳面就是它们：声明键 + 系统注入键放行，未声明
        键拒绝并回显合法键（空 properties 的 browser_snapshot 回显 none）。
        """
        manifest = json.loads(
            (_ROOT / "plugins" / "shared" / "browser" / "plugin.json").read_text(encoding="utf-8")
        )
        schemas = {t["name"]: t["input_schema"] for t in manifest["capabilities"]["tools"]}
        assert schemas, "browser manifest 未声明工具面"
        assert all(s.get("additionalProperties") is False for s in schemas.values()), (
            "browser 声明面不再是严格面：本测试的采纳面前提已变，须重审"
        )

        click_args = {"target": "e12", "doubleClick": True, "session_id": "s-1", "timestamp": "T"}
        assert "pre_decided_results" not in await self._run(
            schemas["browser_click"], click_args
        ), "声明键 + 系统注入键应放行"

        updates = await self._run(schemas["browser_click"], dict(click_args, file_check="x.md"))
        entry = _pre_by_call_id(updates)["call_u1"]
        assert entry["success"] is False
        assert "file_check" in entry["error"]
        assert "target" in entry["error"], "回显合法键：browser_click 的 target"
        assert "doubleClick" in entry["error"]

        # 空 properties（browser_snapshot）：注入键放行，任何模型发出的键拒绝
        snapshot_schema = schemas["browser_snapshot"]
        assert "pre_decided_results" not in await self._run(
            snapshot_schema, {"pipeline_id": "p-1", "workspace": "/ws"}
        )
        snapshot_updates = await self._run(snapshot_schema, {"fullPage": True})
        snapshot_entry = _pre_by_call_id(snapshot_updates)["call_u1"]
        assert snapshot_entry["success"] is False
        assert "fullPage" in snapshot_entry["error"]


class TestActionStatsObservability:
    """参数削减效果观测（2026-10-19）：per-(tool, action, outcome) 计数器。"""

    def test_counters_track_pass_fixed_rejected(self) -> None:
        import asyncio

        plugin = _make_plugin()

        async def _repair_ok(*_a, **_k):
            return None

        # 声明面只有 string 型 path：c3 传 int（可修复形态）→ schema_fixed；
        # c2 缺 path 不可修复 → prefill_rejected；c1 全合规 → pass
        calls = [
            {"name": "file_write", "id": "c1", "args": {"action": "write", "content": "x", "path": "a.txt"}},
            {"name": "file_write", "id": "c2", "args": {"action": "write", "content": "x"}},
            {"name": "file_write", "id": "c3", "args": {"action": "write", "content": "x", "path": 3}},
        ]
        result = asyncio.run(plugin.execute(_make_ctx(calls)))
        stats = result.state_updates["tool_action_stats"]
        # 键为竖线串（e03c29a58 键字符串化：tuple 键过 MCP 边界即炸）
        assert stats["file_write|write|pass"] == 1
        assert stats["file_write|write|prefill_rejected"] == 1
        assert stats["file_write|write|schema_fixed"] == 1
