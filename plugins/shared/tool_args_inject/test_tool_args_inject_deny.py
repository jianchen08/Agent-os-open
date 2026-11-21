# @feature: FP-0.2.〇 管道引擎与插件执行模型（_tool_deny 注入） | @ci: python-coverage
"""tool_args_inject 对动态面双工具注入 ``_tool_deny``。

契约（ADR 2026-11-18-agent-dynamic-tool-deny-list）：state.tool_deny 存在时
注入为 ``_tool_deny``（下划线键 = 服务端权威、模型不可伪造），仅
resource_search / call_tool 两个消费者收到；其余工具不收。state 无键不注入。
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
_SHARED = _DIR.parents[0]

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

_MOD_NAME = "tool_args_inject_deny_test"
_PLUGIN_DIR = _DIR


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MOD_NAME]
        raise
    return module


mod = _load_module()


def _ctx(state: dict[str, Any]) -> Any:
    from _libs.pipeline_base.plugin import PluginContext

    return PluginContext(state=state)


def _state(tool_name: str, **overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": tool_name, "args": {}}],
        "session_id": "s-1",
        "user_id": "u-1",
        "task.id": "t-1",
        "pipeline_id": "t-1",
        "agent_level": "L2",
        "workspace": "/ws/root",
    }
    state.update(overrides)
    return state


async def _run(state: dict[str, Any]) -> dict[str, Any]:
    return await mod.ToolArgsInjectPlugin(config={})._do_work(_ctx(state))


_DENY = ["bash", "phone_control"]


def _tc_args(result: dict[str, Any]) -> dict[str, Any]:
    (tc,) = result["raw_tool_calls"]
    return tc["args"]


async def test_injected_into_both_dynamic_consumers() -> None:
    """resource_search 与 call_tool 都收到 _tool_deny 快照。"""
    for tool in ("resource_search", "call_tool"):
        result = await _run(_state(tool, tool_deny=_DENY))
        assert _tc_args(result).get("_tool_deny") == _DENY, f"{tool} 应收到 deny 快照"


async def test_other_tools_do_not_receive() -> None:
    """非动态面工具不收 deny（不给全工具面无谓增重）。"""
    result = await _run(_state("file_read", tool_deny=_DENY))
    assert "_tool_deny" not in _tc_args(result)


async def test_no_state_key_no_injection() -> None:
    """state 无 tool_deny = 缺席语义 = 不注入键。"""
    result = await _run(_state("call_tool"))
    assert "_tool_deny" not in _tc_args(result)
