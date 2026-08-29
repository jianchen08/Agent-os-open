# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""DelegateDepthGuardPlugin 单元测试——退役后的深度字段初始化行为。

delegate 路由信号已从引擎协议移除，插件仅保留深度字段首次初始化：
- 首轮写入 delegate_depth=0 与 max_delegate_depth=默认上限；
- 已初始化的轮次零副作用（幂等透传）；
- enabled=False 时不写任何字段。
"""

from __future__ import annotations

from typing import Any

import pytest
from pipeline.plugin import PluginContext

pytestmark = pytest.mark.unit


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=state, config={})


def _plugin(config: dict[str, Any] | None = None):
    from plugin import DelegateDepthGuardPlugin  # noqa: PLC0415

    return DelegateDepthGuardPlugin(config=config)


class TestDepthFieldInit:
    def test_first_run_initializes_depth_fields(self) -> None:
        """首轮：写入 delegate_depth=0 与 max_delegate_depth=默认 3。"""
        result = _run(_plugin().execute(_ctx({})))
        assert result.state_updates == {"delegate_depth": 0, "max_delegate_depth": 3}

    def test_config_overrides_max_depth(self) -> None:
        """max_depth 配置进入初始化值。"""
        result = _run(_plugin({"max_depth": 5}).execute(_ctx({})))
        assert result.state_updates == {"delegate_depth": 0, "max_delegate_depth": 5}

    def test_initialized_state_is_noop(self) -> None:
        """深度字段已存在的轮次：零状态更新（幂等）。"""
        state = {"delegate_depth": 0, "max_delegate_depth": 3}
        result = _run(_plugin().execute(_ctx(state)))
        assert result.state_updates == {}

    def test_disabled_writes_nothing(self) -> None:
        """enabled=False：不写任何字段。"""
        result = _run(_plugin({"enabled": False}).execute(_ctx({})))
        assert result.state_updates == {}

    def test_route_signals_empty(self) -> None:
        """不再声明任何路由信号（delegate 信号已退役）。"""
        assert _plugin().route_signals == []


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
