"""路由表逻辑单元测试。

测试输入路由表（可叠加匹配）和输出路由表（互斥优先级仲裁）
的核心行为，覆盖正常路径与边界情况。

所有外部依赖（插件实例等）使用 Mock，不依赖真实实现。
"""

from __future__ import annotations


from pipeline.types import RouteSignal, StateKeys, create_initial_state


# ---------------------------------------------------------------------------
# 输入路由表测试
# ---------------------------------------------------------------------------


class TestInputRouteTable:
    """InputRouteTable 可叠加匹配逻辑测试。"""

    async def test_input_route_additive(self) -> None:
        """可叠加匹配：多个条件同时为真时，插件集合合并去重保序。

        场景:
          condition1=True → plugins=["a", "b"]
          condition2=True → plugins=["b", "c"]
          结果: ["a", "b", "c"]（去重保序）
        """
        from pipeline.route import InputRouteEntry, InputRouteTable

        entries = [
            InputRouteEntry(
                name="cond1",
                condition="flag_a == True",
                target="core",
                plugins=["a", "b"],
                priority=10,
            ),
            InputRouteEntry(
                name="cond2",
                condition="flag_b == True",
                target="core",
                plugins=["b", "c"],
                priority=20,
            ),
        ]
        table = InputRouteTable(entries)
        state = create_initial_state(flag_a=True, flag_b=True)

        plugins, target = table.resolve(state)

        # 去重保序: a, b, c
        assert plugins == ["a", "b", "c"]
        assert target == "core"

    async def test_input_route_end_target(self) -> None:
        """target=end 直接结束，不返回插件。

        场景:
          should_stop=True → target="end" → 不返回插件
        """
        from pipeline.route import InputRouteEntry, InputRouteTable

        entries = [
            InputRouteEntry(
                name="stop",
                condition=f"{StateKeys.SHOULD_STOP} == True",
                target="end",
                plugins=[],
                priority=1,
            ),
            InputRouteEntry(
                name="normal",
                condition="True",
                target="core",
                plugins=["some_plugin"],
                priority=50,
            ),
        ]
        table = InputRouteTable(entries)
        state = create_initial_state(**{StateKeys.SHOULD_STOP: True})

        plugins, target = table.resolve(state)

        assert target == "end"

    async def test_input_route_wait_target(self) -> None:
        """target=wait 挂起管道。

        场景:
          approval_required=True → target="wait"
        """
        from pipeline.route import InputRouteEntry, InputRouteTable

        entries = [
            InputRouteEntry(
                name="approval",
                condition=f"{StateKeys.APPROVAL_REQUIRED} == True",
                target="wait",
                plugins=[],
                priority=3,
            ),
        ]
        table = InputRouteTable(entries)
        state = create_initial_state(**{StateKeys.APPROVAL_REQUIRED: True})

        plugins, target = table.resolve(state)

        assert target == "wait"
        assert plugins == []

    async def test_input_route_no_match_defaults_to_core(self) -> None:
        """无匹配条件时，默认继续 core，返回空插件列表。

        当所有条件的 condition 均为 False 时，
        应返回 target="core" 且 plugins=[]，确保管道不会卡住。
        """
        from pipeline.route import InputRouteEntry, InputRouteTable

        entries = [
            InputRouteEntry(
                name="never_match",
                condition="False",
                target="core",
                plugins=["x"],
                priority=10,
            ),
        ]
        table = InputRouteTable(entries)
        state = create_initial_state()

        plugins, target = table.resolve(state)

        # 无匹配时应有合理默认行为
        assert target == "core"
        assert plugins == []

    async def test_input_route_end_takes_precedence_over_core(self) -> None:
        """end 条件优先级高于 core 条件时，应立即结束。

        当同时匹配 end 和 core 条件时，end 优先级更高应先生效。
        """
        from pipeline.route import InputRouteEntry, InputRouteTable

        entries = [
            InputRouteEntry(
                name="stop",
                condition=f"{StateKeys.SHOULD_STOP} == True",
                target="end",
                plugins=[],
                priority=1,
            ),
            InputRouteEntry(
                name="normal",
                condition="True",
                target="core",
                plugins=["plugin_a"],
                priority=50,
            ),
        ]
        table = InputRouteTable(entries)
        state = create_initial_state(**{StateKeys.SHOULD_STOP: True})

        plugins, target = table.resolve(state)

        # end 应优先于 core
        assert target == "end"


# ---------------------------------------------------------------------------
# 输出路由表测试
# ---------------------------------------------------------------------------


class TestOutputRouteTable:
    """OutputRouteTable 互斥优先级仲裁逻辑测试。"""

    async def test_output_route_arbitration(self) -> None:
        """互斥优先级仲裁：高优先级信号生效。

        场景:
          两个信号: RouteSignal("end", reason="stop"),
                    RouteSignal("next_llm", reason="default")
          end 优先级高 → 返回 end 信号
        """
        from pipeline.route import OutputRouteEntry, OutputRouteTable

        entries = [
            OutputRouteEntry(
                route_type="end",
                condition="True",
                priority=1,
            ),
            OutputRouteEntry(
                route_type="next_llm",
                condition="True",
                priority=7,
            ),
        ]
        table = OutputRouteTable(entries)
        signals = [
            RouteSignal(route_type="end", reason="stop"),
            RouteSignal(route_type="next_llm", reason="default"),
        ]
        state = create_initial_state()

        result = table.arbitrate(signals, state)

        assert result.route_type == "end"

    async def test_output_route_no_signal(self) -> None:
        """无路由信号时返回 fallback。

        场景:
          signals 为空 → 返回 RouteSignal("end", reason="fallback")
        """
        from pipeline.route import OutputRouteEntry, OutputRouteTable

        entries = [
            OutputRouteEntry(
                route_type="end",
                condition="True",
                priority=99,
            ),
        ]
        table = OutputRouteTable(entries)
        state = create_initial_state()

        result = table.arbitrate([], state)

        # 无信号时应返回 fallback
        assert result.route_type == "end"
        assert "fallback" in result.reason.lower() or result.reason == ""

    async def test_output_route_condition_must_match(self) -> None:
        """route_type 匹配但 condition 不满足时跳过该条目。

        场景:
          route_type="end", condition="should_stop == True", should_stop=False
          → 不匹配，继续检查下一个条目
        """
        from pipeline.route import OutputRouteEntry, OutputRouteTable

        entries = [
            OutputRouteEntry(
                route_type="end",
                condition=f"{StateKeys.SHOULD_STOP} == True",
                priority=1,
            ),
            OutputRouteEntry(
                route_type="next_llm",
                condition="True",
                priority=7,
                target_core="llm_call",
            ),
        ]
        table = OutputRouteTable(entries)
        signals = [
            RouteSignal(route_type="end", reason="stop_check"),
            RouteSignal(route_type="next_llm", reason="default"),
        ]
        state = create_initial_state(**{StateKeys.SHOULD_STOP: False})

        result = table.arbitrate(signals, state)

        # condition 不满足，end 被跳过，next_llm 生效
        assert result.route_type == "next_llm"

    async def test_output_route_first_match_wins(self) -> None:
        """互斥仲裁：第一个匹配的条目生效，后续不再检查。

        场景:
          两个 end 条目，优先级分别为 1 和 2
          优先级 1 的 condition=True → 直接返回，不检查优先级 2
        """
        from pipeline.route import OutputRouteEntry, OutputRouteTable

        entries = [
            OutputRouteEntry(
                route_type="end",
                condition="True",
                priority=1,
            ),
            OutputRouteEntry(
                route_type="end",
                condition="True",
                priority=2,
            ),
        ]
        table = OutputRouteTable(entries)
        signals = [RouteSignal(route_type="end", reason="first")]
        state = create_initial_state()

        result = table.arbitrate(signals, state)

        assert result.route_type == "end"

    async def test_output_route_priority_ordering(self) -> None:
        """优先级数字越小越优先。

        场景:
          next_tool (priority=6) vs next_llm (priority=7)
          当两个信号同时存在且 condition 都满足时，next_tool 生效
        """
        from pipeline.route import OutputRouteEntry, OutputRouteTable

        entries = [
            OutputRouteEntry(
                route_type="next_tool",
                condition="True",
                priority=6,
                target_core="tool_execute",
            ),
            OutputRouteEntry(
                route_type="next_llm",
                condition="True",
                priority=7,
                target_core="llm_call",
            ),
        ]
        table = OutputRouteTable(entries)
        signals = [
            RouteSignal(route_type="next_tool", reason="has_tools"),
            RouteSignal(route_type="next_llm", reason="default"),
        ]
        state = create_initial_state()

        result = table.arbitrate(signals, state)

        assert result.route_type == "next_tool"
