# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""delegate_depth_guard 插件剩余分支补测（缺行清零批）。

锁定以下行为契约（对应 plugin.py 缺行）：

1. **name 恒定**（51）：插件标识恒为 "delegate_depth_guard"，与 manifest
   一致（路由/注册按名索引，改名即断链）。
2. **priority 可配**（56）：默认 3（早于 duplicate_check 的 4 与 track 的 15
   执行——深度字段须在消费它的插件之前初始化）；配置值原样生效（含 0）。

覆盖方式：均为经 execute 之外无副作用的纯属性读取，直接实例化断言。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _plugin(config: dict[str, Any] | None = None) -> Any:
    from plugin import DelegateDepthGuardPlugin  # noqa: PLC0415

    return DelegateDepthGuardPlugin(config=config)


class TestIdentity:
    def test_name_matches_manifest_id(self) -> None:
        """插件名恒为 delegate_depth_guard（配置不影响标识）。"""
        assert _plugin().name == "delegate_depth_guard"
        assert _plugin({"depth_key": "x"}).name == "delegate_depth_guard"

    @pytest.mark.parametrize("configured", [0, 1, 7, 99])
    def test_priority_honours_config(self, configured: int) -> None:
        """显式 priority 原值生效（含 0——0 是合法优先级，不得被当假值丢弃）。"""
        assert _plugin({"priority": configured}).priority == configured

    def test_priority_defaults_to_3(self) -> None:
        """缺省优先级 3：先于 duplicate_check(4)/track(15) 初始化深度字段。"""
        assert _plugin().priority == 3
        assert _plugin({}).priority == 3
