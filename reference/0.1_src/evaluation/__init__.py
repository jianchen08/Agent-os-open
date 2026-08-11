"""评估系统 — 统一评估引擎，支持 9 类评估指标。

公共 API：
    MetricType          — 指标类型枚举（tool/agent/human）
    MetricResult        — 单个指标评估结果
    EvaluationResult    — 一次评估的完整结果
    EvaluationConfig    — 评估配置
    MetricDefinition    — 指标定义
    ExpectCondition     — 期望条件
    ExpectSpec          — 期望判断标准
    MetricLoader        — YAML 指标文件加载器
    EvaluationEngine    — 统一评估引擎
    ExpectEvaluator     — 期望值评估器
    ResultMapper        — 评估结果映射器
    EvaluationExecutor  — 评估执行器（与 TaskService 集成）
"""

from evaluation.engine import EvaluationEngine
from evaluation.executor import EvaluationExecutor
from evaluation.expect import ExpectEvaluator
from evaluation.loader import MetricLoader
from evaluation.mapper import ResultMapper
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
    sanitize_eval_paths,
)

__all__ = [
    "EvaluationConfig",
    "EvaluationEngine",
    "EvaluationExecutor",
    "EvaluationResult",
    "ExpectCondition",
    "ExpectEvaluator",
    "ExpectSpec",
    "MetricDefinition",
    "MetricLoader",
    "MetricResult",
    "MetricType",
    "ResultMapper",
]
