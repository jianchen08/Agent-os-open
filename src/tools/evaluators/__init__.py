"""
内置评估器

提供可复用的评估指标执行器，支持：
- 文件存在性检查 (通过 FileEvaluator 工具)
- 内容检查 (通过 FileEvaluator 工具)
- Schema 验证 (通过 SchemaEvaluator 工具)
- 测试运行 (通过 TestEvaluator 工具)

注意：具体的评估功能已迁移到 tools/builtin/evaluators/ 目录下的 Tool 实现

评估结果传播机制：
- 结果包装：统一评估结果格式
- 结果传播：将结果传播到调用方和存储系统
- 结果存储：将评估结果存储到数据库
- 结果查询：提供查询评估结果的 API
"""

# 结果传播机制
from src.tools.evaluators.result_propagator import (
    ResultPropagationCallback,
    ResultPropagator,
    get_global_propagator,
    set_global_propagator,
)
from src.tools.evaluators.result_query import EvaluationResultQuery
from src.tools.evaluators.result_storage import (
    EvaluationResultStorage,
    create_storage_backend,
)

# 评估结果传播机制
from src.tools.evaluators.result_wrapper import (
    EvaluationResult,
    EvaluationSummary,
    ResultWrapper,
)

__all__ = [
    # 结果包装
    "EvaluationResult",
    "EvaluationSummary",
    "ResultWrapper",
    # 结果传播
    "ResultPropagator",
    "ResultPropagationCallback",
    "get_global_propagator",
    "set_global_propagator",
    # 结果存储
    "EvaluationResultStorage",
    "create_storage_backend",
    # 结果查询
    "EvaluationResultQuery",
]
