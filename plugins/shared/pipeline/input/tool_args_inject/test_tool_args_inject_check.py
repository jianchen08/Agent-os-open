# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""tool_args_inject 首轮 injected_params 声明自检测试（ADR 2026-10-02 并入）。

锁定自检契约（警告不阻断）：
- 自有注入键 / config default_params 键 / input_schema.properties 可见参数
  → 视为有来源，不告警；
- 无任何来源的声明 → WARNING（悬空声明检测），警告数 = 未覆盖声明参数数；
- 无论 core_type，首轮实际校验后置 tool.injected_params_checked，再入跳过；
- tool_registry 服务不可用时回退 state["_tool_definitions"] 夹具；
- 两来源皆空 → 不置标记（下轮重试）；注册表可用时以它为权威。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parents[2]  # plugins/shared（pipeline 包）
for _p in (str(_SHARED_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOD_NAME = "tool_args_inject_check_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MOD_NAME]
        raise
    return module


mod = _load_module()
ToolArgsInjectPlugin = mod.ToolArgsInjectPlugin
_CHECK_DONE_KEY = "tool.injected_params_checked"


def _tool(name: str, injected: list[str], visible: list[str] | None = None) -> Any:
    """构造注册表形态的工具定义（SimpleNamespace，属性访问路径）。"""
    schema_props = {p: {"type": "string"} for p in (visible or [])}
    return types.SimpleNamespace(
        name=name,
        injected_params=injected,
        input_schema={"type": "object", "properties": schema_props},
    )


def _dict_tool(name: str, injected: list[str], visible: list[str] | None = None) -> dict[str, Any]:
    """构造 state 夹具形态的工具定义（dict 访问路径）。"""
    schema_props = {p: {"type": "string"} for p in (visible or [])}
    return {
        "name": name,
        "injected_params": injected,
        "input_schema": {"type": "object", "properties": schema_props},
    }


class _FakeRegistry:
    def __init__(self, tools: list[Any]) -> None:
        self._tools = tools

    def list_all(self) -> list[Any]:
        return self._tools


def _make_plugin(config: dict | None = None) -> Any:
    return ToolArgsInjectPlugin(config=config)


def _ctx(state: dict[str, Any], registry: _FakeRegistry | None = None) -> Any:
    from pipeline.plugin import PluginContext

    ctx = PluginContext(state=state)
    if registry is not None:
        ctx._services["tool_registry"] = registry
    return ctx


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "injected_params" in r.getMessage()]


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"core_type": "llm_call"}
    state.update(overrides)
    return state


async def _run(state: dict[str, Any], config: dict | None = None, registry: _FakeRegistry | None = None) -> dict[str, Any]:
    return await _make_plugin(config)._do_work(_ctx(state, registry))


@pytest.mark.parametrize("param", ["tool_record_id", "_retriever", "custom_unknown"])
async def test_uncovered_declaration_warns_and_sets_done(param: str, caplog: pytest.LogCaptureFixture) -> None:
    """无来源声明（对象形态注册表）→ WARNING；实际校验后置完成标记。"""
    with caplog.at_level(logging.WARNING):
        result = await _run(_state(), registry=_FakeRegistry([_tool("t1", [param])]))
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and f"'{param}'" in msgs[0] and "'t1'" in msgs[0]
    assert result[_CHECK_DONE_KEY] is True


async def test_native_key_visible_prop_and_default_params_not_warned(caplog: pytest.LogCaptureFixture) -> None:
    """自有注入键、schema 可见参数、config default_params 键均有来源，零告警。"""
    config = {"default_params": {"t1": {"custom_default": "v"}}}
    with caplog.at_level(logging.WARNING):
        result = await _run(
            _state(),
            config=config,
            registry=_FakeRegistry([_tool("t1", ["session_id", "custom_visible", "custom_default"], visible=["custom_visible"])]),
        )
    assert _warnings(caplog) == []
    assert result[_CHECK_DONE_KEY] is True


async def test_warning_count_equals_uncovered_param_count(caplog: pytest.LogCaptureFixture) -> None:
    """性质断言：警告数与未覆盖声明参数数一一对应（2 悬空 + 1 覆盖 = 2 条）。"""
    with caplog.at_level(logging.WARNING):
        await _run(_state(), registry=_FakeRegistry([_tool("t1", ["tool_record_id", "_retriever", "session_id"])]))
    assert len(_warnings(caplog)) == 2


async def test_check_runs_on_llm_call_and_second_run_skips(caplog: pytest.LogCaptureFixture) -> None:
    """core_type=llm_call 也自检；完成标记合入 state 后再入零动作（不重复告警）。"""
    state = _state()
    with caplog.at_level(logging.WARNING):
        first = await _run(state, registry=_FakeRegistry([_tool("t1", ["tool_record_id"])]))
    assert first[_CHECK_DONE_KEY] is True
    assert _warnings(caplog) != []  # 首轮告警

    state.update(first)  # 模拟引擎轮边界合并 state_updates
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        second = await _run(state, registry=_FakeRegistry([_tool("t1", ["tool_record_id"])]))
    assert _warnings(caplog) == []  # 再入跳过
    assert second == {"tool.params_injected": False}  # 零动作直通，不重复置键


async def test_state_fallback_used_when_registry_absent(caplog: pytest.LogCaptureFixture) -> None:
    """注册表不可用 → 回退 state["_tool_definitions"]（dict 形态夹具）。"""
    state = _state(_tool_definitions={"t1": _dict_tool("t1", ["tool_record_id"])})
    with caplog.at_level(logging.WARNING):
        result = await _run(state)
    assert len(_warnings(caplog)) == 1
    assert result[_CHECK_DONE_KEY] is True


async def test_registry_authoritative_over_state_fixture(caplog: pytest.LogCaptureFixture) -> None:
    """注册表可用时以其为权威：state 夹具中的悬空声明不再参与校验。"""
    state = _state(_tool_definitions={"bad": _dict_tool("bad", ["tool_record_id"])})
    with caplog.at_level(logging.WARNING):
        await _run(state, registry=_FakeRegistry([_tool("t1", ["session_id"])]))
    assert _warnings(caplog) == []


async def test_both_sources_empty_no_done_flag_retries() -> None:
    """注册表与 state 夹具皆空 → 不置标记（零工具会话下轮重试，无副作用）。"""
    result = await _run(_state())
    assert result == {"tool.params_injected": False}
