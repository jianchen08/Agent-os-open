# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""pipeline/_base 三件套（plugin.py / types.py）分支补测（簇 J）。

`_base/` 不在插桩车道基集（`scripts/coverage_exempt.py:BASE_TEST_PATHS`），
本文件需登记 `tests/test_pipeline_base_gaps.py` 才进覆盖面（见回报）。

钉的行为契约：

types.py
- ``create_initial_state`` 产出全字段缺省（枚举值/空表/None/False 混合）+
  ``**overrides`` 覆盖（两组：单键覆盖、多键全量覆盖），且不污染默认值本身；
- ``IPlugin`` 系抽象契约：未实现 name/priority/execute 无法实例化；
  ``IOutputPlugin.route_signals`` 默认空列表（声明性钩子，子类可覆写）；
- ``PluginContext.get_service``：已注册返回同对象（身份），未注册抛 KeyError
  且错误文案含服务名；``_services`` 缺省为独立空 dict（无共享可变默认）；
- ``PluginResult`` / ``OutputResult`` 的 dataclass 缺省独立（非共享列表）。

plugin.py
- ``find_plugin_config`` 三分支：精确命中、名称前缀（plugin_name 以 key+_ 开头）、
  键前缀（key 以 plugin_name+_ 开头）、空字典短路、无匹配返回空 dict。

全部为纯数据/纯函数面，零 mock。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from pipeline._base.plugin import (  # noqa: E402
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    IPlugin,
    OutputResult,
    PluginContext,
    PluginResult,
    find_plugin_config,
)
from pipeline._base.types import (  # noqa: E402
    StateKeys,
    TargetType,
    create_initial_state,
)

# ══════════════════ create_initial_state ══════════════════


class TestCreateInitialState:
    def test_defaults_cover_all_declared_keys(self) -> None:
        """缺省态：所有 StateKeys 常量都有初始值，核心类型为 llm_call。"""
        state = create_initial_state()

        for key in (
            StateKeys.ITERATION,
            StateKeys.CORE_TYPE,
            StateKeys.ENDED,
            StateKeys.SESSION_ID,
            StateKeys.TASK_ID,
            StateKeys.AGENT_LEVEL,
            StateKeys.RAW_RESULT,
            StateKeys.RAW_ERROR,
            StateKeys.RAW_TOOL_CALLS,
            StateKeys.RAW_THINKING,
            StateKeys.TOOL_RESULTS,
            StateKeys.SHOULD_STOP,
            StateKeys.CONVERSATION_MODE,
        ):
            assert key in state, f"初始状态缺字段 {key}"

        assert state[StateKeys.CORE_TYPE] == TargetType.LLM_CALL.value
        assert state[StateKeys.ITERATION] == 0
        assert state[StateKeys.AGENT_LEVEL] == "L1"
        assert state[StateKeys.ENDED] is False

    @pytest.mark.parametrize(
        ("overrides", "expect"),
        [
            ({StateKeys.ITERATION: 7}, {StateKeys.ITERATION: 7, StateKeys.AGENT_LEVEL: "L1"}),
            (
                {StateKeys.CORE_TYPE: TargetType.TOOL_EXECUTE.value, StateKeys.SHOULD_STOP: True},
                {StateKeys.CORE_TYPE: TargetType.TOOL_EXECUTE.value, StateKeys.SHOULD_STOP: True},
            ),
        ],
    )
    def test_overrides_win(self, overrides: dict[str, Any], expect: dict[str, Any]) -> None:
        """覆盖语义：显式入参胜出，未覆盖键保留缺省（单键/多键两组）。"""
        state = create_initial_state(**overrides)

        for key, value in expect.items():
            assert state[key] == value

    def test_overrides_do_not_leak_into_next_call(self) -> None:
        """性质：两次调用互不影响（每次新建字典，无跨调用状态）。"""
        first = create_initial_state(**{StateKeys.ITERATION: 99})
        second = create_initial_state()

        assert first[StateKeys.ITERATION] == 99
        assert second[StateKeys.ITERATION] == 0

    def test_seeded_lists_are_not_shared(self) -> None:
        """性质：列表字段每次新建实例（追加不串场）。"""
        a = create_initial_state()
        b = create_initial_state()
        a[StateKeys.RAW_TOOL_CALLS].append({"id": "x"})

        assert b[StateKeys.RAW_TOOL_CALLS] == []
        assert a[StateKeys.TOOL_RESULTS] is not b[StateKeys.TOOL_RESULTS]


# ══════════════════ 抽象接口契约 ══════════════════


class TestAbstractContracts:
    @pytest.mark.parametrize("base_cls", [IPlugin, IInputPlugin, ICorePlugin, IOutputPlugin])
    def test_abstract_cannot_instantiate(self, base_cls: type) -> None:
        """四个接口都不可直接实例化（抽象契约，四组输入）。"""
        with pytest.raises(TypeError):
            base_cls()  # type: ignore[abstract]

    def test_output_plugin_default_route_signals_empty(self) -> None:
        """route_signals 默认空列表；子类覆写后按声明返回（声明性钩子）。"""
        assert IOutputPlugin.route_signals.fget(None) == []  # type: ignore[attr-defined]

        class _Declaring(IOutputPlugin):
            @property
            def name(self) -> str:
                return "declaring"

            @property
            def priority(self) -> int:
                return 1

            @property
            def route_signals(self) -> list[str]:
                return ["retry", "stop"]

            async def execute(self, ctx: PluginContext) -> OutputResult:
                return OutputResult()

        assert _Declaring().route_signals == ["retry", "stop"]


# ══════════════════ PluginContext.get_service ══════════════════


class TestPluginContextServices:
    def test_registered_service_returned_by_identity(self) -> None:
        """已注册服务按名返回同对象（identity，非拷贝）。"""
        marker = object()
        ctx = PluginContext(state={}, _services={"event-bus": marker})

        assert ctx.get_service("event-bus") is marker

    def test_unknown_service_raises_key_error_with_name(self) -> None:
        """未注册服务抛 KeyError 且文案含服务名（fail-closed 可排障）。"""
        ctx = PluginContext(state={})

        with pytest.raises(KeyError) as exc:
            ctx.get_service("pipeline-executor")

        assert "pipeline-executor" in str(exc.value)

    def test_default_services_are_per_instance(self) -> None:
        """性质：缺省 _services 每实例独立（一处注册不影响另一上下文）。"""
        a = PluginContext(state={})
        b = PluginContext(state={})
        a._services["x"] = 1

        assert "x" not in b._services
        assert PluginContext(state={}).config == {}


# ══════════════════ PluginResult / OutputResult ══════════════════


class TestResultDataclasses:
    def test_defaults_are_independent_per_instance(self) -> None:
        """缺省字段每实例独立；继承语义：OutputResult 是 PluginResult 子类。"""
        r1 = PluginResult()
        r2 = PluginResult()
        r1.state_updates["k"] = 1

        assert r2.state_updates == {}
        assert r1.skip_remaining is False and r1.error is None
        assert isinstance(OutputResult(), PluginResult)

    def test_error_carries_exception_value(self) -> None:
        """error 字段承载异常对象本身（调用方可读类型与消息）。"""
        boom = ValueError("bad state")
        result = PluginResult(state_updates={"done": True}, skip_remaining=True, error=boom)

        assert result.error is boom
        assert result.skip_remaining is True
        assert result.state_updates == {"done": True}


# ══════════════════ find_plugin_config ══════════════════


class TestFindPluginConfig:
    def test_empty_registry_short_circuits(self) -> None:
        """空配置表 → 空 dict（不抛，调用方零分支）。"""
        assert find_plugin_config("isolation_guard", {}) == {}

    def test_exact_match_wins(self) -> None:
        """精确匹配优先于任何前缀匹配。"""
        configs = {
            "isolation_guard": {"exact": True},
            "isolation": {"prefix_candidate": True},
            "isolation_guard_extra": {"key_prefix_candidate": True},
        }

        assert find_plugin_config("isolation_guard", configs) == {"exact": True}

    def test_name_starts_with_key_prefix(self) -> None:
        """名称前缀命中：plugin_name 以 ``key + "_"`` 开头。"""
        configs = {"level": {"from_level_key": True}}

        assert find_plugin_config("level_guard", configs) == {"from_level_key": True}

    def test_key_starts_with_name_prefix(self) -> None:
        """键前缀命中：key 以 ``plugin_name + "_"`` 开头。"""
        configs = {"tool_cache_writer_v2": {"from_key_prefix": True}}

        assert find_plugin_config("tool_cache_writer", configs) == {"from_key_prefix": True}

    def test_no_match_returns_empty(self) -> None:
        """无匹配 → 空 dict；且近失配（缺边界下划线）不算命中。"""
        configs = {"isolation_guardrails": {"nope": True}, "other": {}}

        assert find_plugin_config("isolation_guard", configs) == {}
        assert find_plugin_config("guard", {"isolation_guard": {}}) == {}

    def test_property_returned_config_is_registry_entry(self) -> None:
        """性质：返回对象即登记值本身（可后续热更新共享同一 dict）。"""
        entry = {"hot": "reload"}
        configs = {"plugin": entry}

        assert find_plugin_config("plugin", configs) is entry
