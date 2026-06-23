"""
Round2 Pipeline 管道引擎模块 — 测试缺口补充。

覆盖以下 AC 的边界与深度验证：
- F-PIP-04 / AC-PIP-11: 终态 Output 插件链在管道结束后执行
- F-PIP-07: 输入路由 target=wait 解析
- F-PIP-08: 五种 route_type 完整仲裁
- F-PIP-09 / AC-PIP-12: 路由条件安全解析
- F-PIP-13 / AC-PIP-05: 四种错误策略边界
- F-PIP-06: Output 路由表插件解析
"""
from tests.unit.test_round2_agent_pipeline_gaps import (  # noqa: F401
    TestPostEndOutputChainDeep,
    TestInputRouteWaitTarget,
    TestOutputRouteAllFiveTypes,
    TestConditionParserAdvancedSecurity,
    TestErrorPolicyMixedAndBoundary,
    TestOutputRoutePluginResolution,
)
