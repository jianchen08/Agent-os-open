"""
Round2 测试系统 — 评估门禁测试缺口补充。

覆盖以下 AC 的边界与深度验证：
- AC-TST-05: 评估门禁通过才能标记完成（门禁阻止验证）
"""
from tests.unit.test_round2_infra_gaps import (  # noqa: F401
    TestEvaluationGateEnforcement,
)
