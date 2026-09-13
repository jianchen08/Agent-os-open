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

import json

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "tool_schema_validator")

import plugin as _tsv_plugin_mod  # noqa: E402  模块级捕获：测试内 re-import 会被裸名逐出干扰
from typing import Any

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

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
    add_plugin_dir("input", "tool_schema_validator")
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


def _tool_msgs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("role") == "tool"]


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

        # 不进入 validated_calls → tool_core 不会执行
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert remaining == []

        # 注入了诊断 tool 消息
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        content = tool_msgs[0]["content"]
        assert "SCHEMA_VALIDATION_FAILED" in content
        assert "Missing required field: action" in content

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
        messages = result.state_updates.get("messages", [])

        # assistant(tool_calls) 仍在，其后紧跟 role=tool 诊断
        assert any(m["role"] == "assistant" and m.get("tool_calls") for m in messages)
        tool_msgs = _tool_msgs(messages)
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_9"

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

        assert updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert len(tool_msgs) == 1
        assert "Type mismatch for 'action'" in tool_msgs[0]["content"]


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
        # 不注入诊断 tool 消息
        assert _tool_msgs(updates.get("messages", [])) == []

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

        # 只有 good 进入 validated_calls
        remaining = updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [tc["id"] for tc in remaining] == ["good"]

        # 只为 bad 注入诊断消息
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "bad"


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

        # 仍拦截（缺必填 action）
        assert updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert len(tool_msgs) == 1
        content = tool_msgs[0]["content"]
        # 截断引导文案出现
        assert "max_tokens 被截断" in content
        assert "append" in content
        # 透传 output_truncated 标志
        assert '"output_truncated":true' in content.replace(" ", "")

    @pytest.mark.asyncio
    async def test_no_truncation_signal_no_hint(self) -> None:
        """无截断信号时，缺失字段的诊断不含截断引导（避免误导）。"""
        plugin = _make_plugin()
        tc = {"name": "file_write", "args": {"path": "/a"}, "id": "call_n"}
        ctx = _make_ctx([tc], output_truncated=False)

        result = await plugin.execute(ctx)
        updates = result.state_updates
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert len(tool_msgs) == 1
        assert "max_tokens 被截断" not in tool_msgs[0]["content"]


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
        # registry 里有 file_write 定义 → 能识别缺 action 并拦截
        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS, []) == []

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
        # 回退路径也能拦截缺 action
        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS, []) == []


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
        assert updates.get(StateKeys.RAW_TOOL_CALLS, []) == [], "坏参数应被拦截"
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert tool_msgs and "SCHEMA_VALIDATION_FAILED" in tool_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_invalid_array_string_blocked_as_type_error(self) -> None:
        """非 JSON 的 array 参数 → 保留原值 → 拦截 + 诊断（不再伪装空 list）。"""
        plugin = _make_plugin()
        tc = {"name": "options_tool", "args": {"options": {}, "tags": "a, b"}, "id": "c_arr"}
        result = await plugin.execute(self._ctx_with(tc))
        updates = result.state_updates
        assert updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert tool_msgs and "SCHEMA_VALIDATION_FAILED" in tool_msgs[0]["content"]

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
        add_plugin_dir("input", "tool_schema_validator")
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

        # state 回退定义生效：缺 action 照常拦截
        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        assert result.state_updates.get("schema_errors")

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

        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        errors = result.state_updates.get("schema_errors", [])
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

        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        tool_msgs = _tool_msgs(result.state_updates.get("messages", []))
        assert tool_msgs and f"Type mismatch for '{field}'" in tool_msgs[0]["content"]


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

        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        tool_msgs = _tool_msgs(result.state_updates.get("messages", []))
        assert tool_msgs and "Type mismatch for 'flag'" in tool_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_hostile_string_subclass_caught_by_conversion_guard(self) -> None:
        """值是行为异常的 str 子类（.lower() 抛错）→ 转换守卫兜底保原值，不外溢。"""

        class HostileStr(str):
            def lower(self) -> str:
                raise RuntimeError("hostile lower")

        result = await self._run(HostileStr("true"))

        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        tool_msgs = _tool_msgs(result.state_updates.get("messages", []))
        assert tool_msgs and "SCHEMA_VALIDATION_FAILED" in tool_msgs[0]["content"]


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
        assert fixes and all("memo" not in f for fix in fixes for f in fix["fixes"])

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

        assert updates.get(StateKeys.RAW_TOOL_CALLS, []) == []
        assert "schema_errors" not in updates  # 截断不是 schema 错误，走独立诊断
        tool_msgs = _tool_msgs(updates.get("messages", []))
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "c_t"
        payload = json.loads(tool_msgs[0]["content"])
        assert payload["success"] is False
        assert payload["error_code"] == "ARGS_TRUNCATED"
        assert payload["lost_keys"] == ["goal"]

    @pytest.mark.asyncio
    async def test_truncation_skips_only_the_truncated_call(self, tsv: _FreshModule) -> None:
        """同一批混合调用：截断的拦下，完好的照常放行。"""
        tsv.mod.set_capability_caller(_StubRepairCaller(repaired=self._GOOD_REPAIR))
        bad = {"name": "file_write", "args": '{"path": "/a", "goal":', "id": "c_bad"}
        good = {"name": "file_write", "args": {"action": "write", "path": "/b"}, "id": "c_good"}

        result = await tsv.plugin().execute(
            _state_ctx({StateKeys.RAW_TOOL_CALLS: [bad, good]})
        )

        remaining = result.state_updates.get(StateKeys.RAW_TOOL_CALLS, [])
        assert [tc["id"] for tc in remaining] == ["c_good"]


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

