# @feature: FP-T07 llm api | @ci: python-coverage
"""llm.complete_stream 服务侧 msg 标准化唯一关卡——集成行为测试。

契约（T7 LLM 调用面统一）：任何消费方（llm_core 主轮 / guard 压缩 / 未来
任何 LLM 消费方）送入带孤儿 tool result / 未配对 assistant 的 messages，
实际发往 adapter 的载荷必须配对完整——每条 tool 消息的 tool_call_id 都能
被其前方最近的 assistant(tool_calls) 认领，且无未应答的 assistant(tool_calls)。

测试断行为（输入 → adapter 收到的载荷），mock 仅用于外部依赖
（adapter = 跨进程 LLM 上游边界）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_MOD_NAME = "llm_server_normalize_gate_test"


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。"""
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None, "cannot load llm plugin server.py"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


class FakeBus:
    async def notify(self, method: str, params: dict[str, Any]) -> None:  # noqa: ARG002
        return None


class RecordingAdapter:
    """记录 completion 收到kwargs 的伪 adapter（外部依赖替身）。"""

    def __init__(self, text: str = "ok") -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    async def completion(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        return SimpleNamespace(
            text=self._text,
            tool_calls=[],
            thinking_text=None,
            usage=None,
            finish_reason="stop",
        )

    async def health_check(self, model: str) -> bool:
        return True


def _assistant(call_ids: str | list[str]) -> dict[str, Any]:
    ids = [call_ids] if isinstance(call_ids, str) else call_ids
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "f", "arguments": "{}"}}
            for cid in ids
        ],
    }


def _tool_result(call_id: str, content: str = "ok") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _pair_violations(messages: list[dict[str, Any]]) -> list[str]:
    """配对完整性性质检查（返回空列表 = 配对完整）。

    不变量：
    1. 每条 tool 消息的 tool_call_id 必须能被其前方最近的
       assistant(tool_calls) 认领（且未被同轮其他 tool 消费）；
    2. 不存在未应答的 assistant(tool_calls)（所有 id 都有后续 tool 消息）。
    """
    violations: list[str] = []
    expecting: set[str] = set()
    answered: set[str] = set()
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            if expecting:
                violations.append(f"msg[{i}]: 前轮 assistant(tool_calls) 未应答 {expecting}")
            expecting = {tc.get("id") for tc in msg.get("tool_calls", []) if tc.get("id")}
        elif msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id not in expecting:
                violations.append(f"msg[{i}]: tool result tool_call_id={tc_id} 无配对 assistant")
            else:
                expecting.discard(tc_id)
                answered.add(tc_id)
    if expecting:
        violations.append(f"尾部 assistant(tool_calls) 未应答 {expecting}")
    return violations


def _run_through_server(
    mod: Any,
    messages: list[dict[str, Any]],
    *,
    model: str = "normalize-gate-model",
) -> list[dict[str, Any]]:
    """经 llm_complete_stream 全链路，返回 adapter 实际收到的 messages。"""
    mod.set_config({"llm": {"models": {model: {"provider": "deepseek"}}}})
    try:
        bus = FakeBus()

        class _BusPlugin:
            @staticmethod
            def get_capability(name: str) -> Any:
                if name == "event-bus":
                    return bus
                raise KeyError(name)

        adapter = RecordingAdapter()
        mod._adapter = adapter
        mod.plugin.get_capability = _BusPlugin.get_capability  # type: ignore[method-assign]
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                mod.llm_complete_stream(
                    model=model,
                    messages=messages,
                    _call_context={"thread_id": "t", "pipeline_id": "p", "message_id": "m"},
                )
            )
        finally:
            loop.close()
        assert adapter.calls, "adapter 未被调用"
        return adapter.calls[0]["messages"]
    finally:
        mod.set_config({})


class TestCompleteStreamNormalizeGate:
    """complete_stream 入口收到带孤儿 messages → 发往 adapter 的载荷配对完整。"""

    def test_orphan_tool_result_at_head_stripped(self) -> None:
        """输入组 1：头部孤儿 tool result + 完好一轮 → 孤儿被摘，完好轮保留。"""
        mod = _load_server()
        inbound = [
            _tool_result("call_orphan", "上个 run 的遗留结果"),
            _assistant("call_aaa"),
            _tool_result("call_aaa"),
            {"role": "user", "content": "继续"},
        ]
        payload = _run_through_server(mod, inbound)
        assert payload[0]["role"] != "tool"  # 孤儿不再打头
        assert len(payload) == 3
        assert _pair_violations(payload) == []

    def test_incomplete_assistant_round_removed(self) -> None:
        """输入组 2：中途半轮（assistant 两个 tool_call 只有一个结果）→ 半轮整删。"""
        mod = _load_server()
        inbound = [
            {"role": "user", "content": "q"},
            _assistant(["call_1", "call_2"]),
            _tool_result("call_1"),  # call_2 的结果因工具中断缺失
            _assistant("call_3"),
            _tool_result("call_3"),
        ]
        payload = _run_through_server(mod, inbound)
        # 半轮（assistant call_1/call_2 + 已到的 call_1 结果）整轮移除
        assert [m.get("role") for m in payload] == ["user", "assistant", "tool"]
        assert payload[-1]["tool_call_id"] == "call_3"
        assert _pair_violations(payload) == []

    def test_clean_history_passes_through_pair_complete(self) -> None:
        """对照组：本就配对完整的历史 → 原样通过（条数不变，不误删）。"""
        mod = _load_server()
        inbound = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            _assistant("call_a"),
            _tool_result("call_a"),
            {"role": "user", "content": "q2"},
        ]
        payload = _run_through_server(mod, inbound)
        assert len(payload) == len(inbound)
        assert _pair_violations(payload) == []

    def test_minimax_provider_nonfirst_system_converted(self) -> None:
        """输入组 3（provider 区分）：minimax 模型 → 非首位 system 到达载荷前转 user。"""
        mod = _load_server()
        model = "normalize-gate-minimax"
        inbound = [
            {"role": "system", "content": "first"},
            {"role": "system", "content": "reminder injected by pipeline"},
        ]
        mod.set_config({"llm": {"models": {model: {"provider": "minimax"}}}})
        try:
            bus = FakeBus()

            class _BusPlugin:
                @staticmethod
                def get_capability(name: str) -> Any:
                    if name == "event-bus":
                        return bus
                    raise KeyError(name)

            adapter = RecordingAdapter()
            mod._adapter = adapter
            mod.plugin.get_capability = _BusPlugin.get_capability  # type: ignore[method-assign]
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    mod.llm_complete_stream(model=model, messages=inbound)
                )
            finally:
                loop.close()
            payload = adapter.calls[0]["messages"]
        finally:
            mod.set_config({})
        assert payload[0]["role"] == "system"  # 首位保留
        assert payload[1]["role"] == "user"  # 非首位 system 不出服务边界

    def test_property_pair_complete_over_shapes(self) -> None:
        """性质断言：无论入口多脏，adapter 载荷恒满足配对完整性不变量。"""
        mod = _load_server()
        dirty_shapes = [
            [_tool_result("call_x1")],
            [
                _assistant("call_y1"),
                _tool_result("call_z1"),  # 失配
                _tool_result("call_y1"),
            ],
            [
                _tool_result("call_w0"),
                _assistant(["call_w1", "call_w2"]),
                _tool_result("call_w1"),
                {"role": "user", "content": "next"},
                _assistant("call_w3"),
                _tool_result("call_w3"),
            ],
        ]
        for inbound in dirty_shapes:
            payload = _run_through_server(mod, inbound)
            assert _pair_violations(payload) == [], f"输入 {inbound} 产出带病载荷 {payload}"


class TestRepairJsonService:
    """llm.repair_json 能力服务（repair_json_string 能力面收敛点）。"""

    def test_repairable_truncated_json_returns_repaired(self) -> None:
        mod = _load_server()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(mod.llm_repair_json('{"path": "/tmp/f'))
        finally:
            loop.close()
        assert result["repaired"] == '{"path": "/tmp/f"}'

    def test_unrepairable_garbage_returns_none(self) -> None:
        mod = _load_server()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(mod.llm_repair_json("not json at all"))
        finally:
            loop.close()
        assert result["repaired"] is None

    def test_valid_json_passes_through(self) -> None:
        mod = _load_server()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(mod.llm_repair_json('{"a": 1}'))
        finally:
            loop.close()
        assert result["repaired"] == '{"a": 1}'
