"""
Round2 Config 配置管理模块 — 测试缺口补充。

覆盖以下 AC 的边界与深度验证：
- AC-CFG-03: ${ENV_VAR}替换（边界：多变量/嵌套列表/空默认值）
- AC-CFG-06: API Key不在YAML硬编码（静态扫描真实配置文件）
"""
from tests.unit.test_round2_infra_gaps import (  # noqa: F401
    TestEnvVarSubstitutionEdge,
    TestApiKeyStaticScan,
)
