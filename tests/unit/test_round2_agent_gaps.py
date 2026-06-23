"""
Round2 Agent 编排模块 — 测试缺口补充。

覆盖以下 AC 的边界与深度验证：
- F-AGT-10: dynamic_vars 每轮注入到最后一条消息
- F-AGT-11: reference / literal / expression 三种类型在 to_state 中完整字段保留
- F-AGT-13: tool_ids 限制
- F-AGT-15: input_schema / output_schema 在 to_state 中的传递
- AgentPluginsConfig: disabled / enabled 合并语义
- F-AGT-04: 热替换后新 Agent 配置正确生效
- F-PIP-02 关联: max_iterations 传递
"""
from tests.unit.test_round2_agent_pipeline_gaps import (  # noqa: F401
    TestDynamicVarsToStateSerialization,
    TestContextVarTypesInToState,
    TestContextBuilderFolderType,
    TestToolIdsSerialization,
    TestAgentPluginsConfigMerge,
    TestHotSwapNewConfig,
    TestMaxIterationsPropagation,
)
