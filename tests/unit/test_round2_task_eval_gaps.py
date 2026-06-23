"""
Round2 Task 任务管理 + Evaluation 评估模块 — 测试缺口补充。

覆盖以下 AC 的边界与深度验证：
- AC-TASK-01: 7种状态转换路径合法/非法（验证完整状态机覆盖率）
- AC-TASK-03: 容器任务管理子任务（状态独立性验证）
- AC-EVAL-02: 11种操作符全覆盖（ExpectConditionEvaluator）
- AC-EVAL-03: 嵌套字段路径解析
- AC-EVAL-05: 评估不通过→failed（完整流程）
"""
from tests.unit.test_round2_infra_gaps import (  # noqa: F401
    TestStateMachineCoverageIntegrity,
    TestContainerTaskStateIsolation,
    TestExpectConditionEvaluatorAllOps,
    TestNestedFieldResolutionEvalCond,
    TestEvaluationFailFlow,
)
