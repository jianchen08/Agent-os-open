"""tool_schema_validator 拦截契约 — 单元测试。

验证核心契约：缺 required 参数 / 无法自动修复的类型不匹配时，
调用被拦截（不进入 validated_calls，tool_core 不会执行），并注入一条
role=tool 诊断消息把缺失/类型错误明细反馈给 LLM，使其能补齐参数
而非盲目重试（与截断检测 _check_args_truncation 范式一致）。

回归场景：模型调用 file_write 时漏传 action，工具拿到 action=None
返回 "不支持的操作: None" 这一不透明错误，导致 LLM 无限重复调用。
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys


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
    from plugins.input.tool_schema_validator.plugin import ToolSchemaValidator
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
        修复后仍失败 → 拦截。
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
        # 修复后的 action 应为字符串 "123"
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
