# 评估模块

## 需求

### 职责
提供标准化的评估框架，支持多种评估器类型（工具评估、工作流评估、人工评估）和评分策略，用于验证任务执行结果。

### 对外接口
- 输入：评估指标配置、评估上下文
- 输出：评估结果（通过/失败、评分、证据、建议）

### 依赖
- 依赖模块：`src.tools.executor`（工具执行器）、`src.tools.registry`（工具注册表）
- 外部依赖：Pydantic、PyYAML

### 评估指标存储
评估指标存储在 `config/evaluation_metrics/` 目录下的 YAML 文件中，通过 `MetricLoader` 加载。
- 支持内存缓存
- 支持热重载
- 新增 `expect` 字段用于断言规则

## 逻辑

### 流程设计
```
评估请求 → 参数映射 → 选择评估器类型
                           ↓
            ┌──────────────┼──────────────┐
            ↓              ↓              ↓
        工具评估       工作流评估      人工评估
            ↓              ↓              ↓
        执行工具       执行工作流    创建审核任务
            ↓              ↓              ↓
            └──────────────┼──────────────┘
                           ↓
                     输出映射 → 返回评估结果
```

### 数据流向
1. 参数构建：评估上下文 → Jinja2 模板渲染 → 工具/工作流输入
2. 执行分发：评估器类型 → 对应执行器 → 执行结果
3. 结果映射：执行结果 → 输出映射 → EvaluationResult

### 数据模型
#### 评估结果
| 字段 | 类型 | 说明 |
|------|------|------|
| metric_id | str | 指标 ID |
| passed | bool | 是否通过 |
| score | float | 评分（0-100） |
| status | EvaluationStatus | 评估状态 |
| message | str | 结果消息 |
| evidence | list[str] | 证据列表 |
| suggestions | list[str] | 改进建议 |

#### 评估上下文
| 字段 | 类型 | 说明 |
|------|------|------|
| task_id | str | 任务 ID |
| task_name | str | 任务名称 |
| expected_output | Any | 预期输出 |
| actual_output | Any | 实际输出 |
| acceptance_criteria | list[str] | 验收标准 |

### API设计
#### 模块API
| 接口 | 职责 |
|------|------|
| `UnifiedEvaluationEngine.evaluate(metric_config, context, timeout) -> EvaluationResult` | 执行单个评估 |
| `UnifiedEvaluationEngine.evaluate_batch(metrics, context, parallel, timeout) -> BatchEvaluationSummary` | 批量评估 |
| `EvaluationEngine.calculate_score(metrics, strategy, weights, threshold) -> float` | 计算综合评分 |

### 配置设计
#### 评估器配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| evaluator_id | 评估器 ID | - |
| evaluator_type | 评估器类型 | tool |
| scoring_strategy | 评分策略 | weighted_average |
| threshold | 通过阈值 | 60.0 |
| timeout_seconds | 超时时间 | 30.0 |
| max_retries | 最大重试次数 | 0 |

#### 评分策略
| 策略 | 说明 |
|------|------|
| average | 简单平均 |
| weighted_average | 加权平均 |
| minimum | 最低分（一票否决） |
| maximum | 最高分 |
| threshold | 阈值法 |
| red_line_priority | 红线优先 |

### 错误处理
- 评估超时：返回 TIMEOUT 状态
- 评估异常：返回 ERROR 状态，记录错误信息
- 工具不存在：返回 ERROR 状态

### 安全设计
- 评估执行有超时控制
- 并发评估有信号量限制
- 红线指标失败导致整体失败

## 结构

### 组件清单（文件夹 - 抽象说明）
无子组件，为扁平结构。

### 文件清单（代码文件 - 具体接口）

#### __init__.py
职责：模块入口，导出公共接口
暴露接口：
- `Evaluator`：评估器抽象基类
- `EvaluationContext`：评估上下文数据类
- `EvaluationResult`：评估结果 Pydantic 模型
- `MetricResult`：单个指标评估结果 Pydantic 模型
- `EvaluationStatus`：评估状态枚举
- `EvaluatorType`：评估器类型枚举
- `EvaluationMode`：评估模式枚举
- `EvaluatorConfig`：评估器配置类
- `ScoringStrategy`：评分策略枚举
- `AggregationStrategy`：结果聚合策略枚举
- `ParameterMapper`：参数映射器类
- `ContextBuilder`：上下文构建器类
- `UnifiedEvaluationEngine`：统一评估引擎类
- `BatchEvaluationSummary`：批量评估汇总数据类
- `EvaluationEngine`：评估引擎类
- `EvaluationError`：评估错误异常
- `MetricLoader`：评估指标文件加载器
- `get_metric_loader`：获取全局 MetricLoader 实例

#### base.py
职责：评估系统基础模块
暴露接口：
- `EvaluatorType`：评估器类型枚举（TOOL/WORKFLOW/HUMAN）
- `EvaluationStatus`：评估状态枚举（PENDING/EVALUATING/PASSED/FAILED/TIMEOUT/ERROR）
- `MetricResult`：单个指标评估结果 Pydantic 模型
- `EvaluationResult`：评估结果 Pydantic 模型
- `EvaluationContext`：评估上下文数据类
- `Evaluator`：评估器抽象基类
- `Evaluator.__init__(evaluator_id: str, config: dict[str, Any] | None)`：初始化评估器
- `Evaluator.evaluator_id`：获取评估器ID（property）
- `Evaluator.evaluator_type`：获取评估器类型（property, abstract）
- `Evaluator.evaluate(context: EvaluationContext) -> EvaluationResult`：执行评估（abstract, async）
- `Evaluator.pre_evaluate(context: EvaluationContext) -> EvaluationContext`：评估前预处理（async）
- `Evaluator.post_evaluate(context: EvaluationContext, result: EvaluationResult) -> EvaluationResult`：评估后处理（async）
- `Evaluator.run(context: EvaluationContext) -> EvaluationResult`：运行完整评估流程（async）
- `EvaluationError`：评估错误异常

#### types.py
职责：评估系统类型定义
暴露接口：
- `ScoringStrategy`：评分策略枚举
- `AggregationStrategy`：结果聚合策略枚举
- `EvaluationMode`：评估模式枚举
- `EvaluatorConfig`：评估器配置数据类
- `EvaluatorConfig.to_dict() -> dict[str, Any]`：转换为字典格式
- `EvaluatorConfig.from_dict(data: dict[str, Any]) -> EvaluatorConfig`：从字典创建配置
- `EvaluatorConfig.get_metric_weight(metric_id: str) -> float`：获取指标权重
- `EvaluatorConfig.set_metric_weight(metric_id: str, weight: float) -> None`：设置指标权重
- `EvaluatorConfig.is_red_line_metric(metric_id: str) -> bool`：检查是否为红线指标
- `EvaluatorConfig.add_red_line_metric(metric_id: str) -> None`：添加红线指标
- `EvaluatorConfig.remove_red_line_metric(metric_id: str) -> None`：移除红线指标
- `EvaluationSummaryConfig`：评估摘要配置数据类
- `EvaluationMetricsConfig`：评估指标配置数据类
- `EvaluatorConfiguration`：EvaluatorConfig 类型别名（向后兼容）

#### engine.py
职责：评估引擎
暴露接口：
- `EvaluationNode`：评估节点数据类
- `BatchEvaluationResult`：批量评估结果数据类
- `BatchEvaluationResult.add_result(result: EvaluationResult) -> None`：添加评估结果
- `BatchEvaluationResult.to_dict() -> dict[str, Any]`：转换为字典格式
- `EvaluationEngine`：评估引擎类
- `EvaluationEngine.__init__(max_concurrent: int, default_timeout: float, enable_logging: bool)`：初始化评估引擎
- `EvaluationEngine.evaluate(evaluator: Evaluator, context: EvaluationContext, timeout: float | None) -> EvaluationResult`：执行单个评估（async）
- `EvaluationEngine.evaluate_batch(evaluators: list[tuple[Evaluator, EvaluationContext]], mode: EvaluationMode, timeout: float | None) -> BatchEvaluationResult`：批量评估（async）
- `EvaluationEngine.evaluate_with_dependencies(evaluations: list[EvaluationNode], timeout: float | None) -> BatchEvaluationResult`：带依赖关系的评估（async）
- `EvaluationEngine.calculate_score(metrics: list[MetricResult], strategy: ScoringStrategy, weights: dict[str, float] | None, threshold: float) -> float`：计算综合评分

#### unified_engine.py
职责：统一评估引擎
暴露接口：
- `BatchEvaluationSummary`：批量评估汇总数据类
- `BatchEvaluationSummary.to_dict() -> dict[str, Any]`：转换为字典格式
- `UnifiedEvaluationEngine`：统一评估引擎类
- `UnifiedEvaluationEngine.__init__(session: AsyncSession | None, tool_registry: ToolRegistry | None, workflow_executor: Callable | None, default_timeout: float, max_concurrent: int)`：初始化统一评估引擎
- `UnifiedEvaluationEngine.evaluate(metric_config: dict[str, Any], context: dict[str, Any], timeout: float | None) -> EvaluationResult`：执行单个评估（async）
- `UnifiedEvaluationEngine.evaluate_batch(metrics: list[dict[str, Any]], context: dict[str, Any], parallel: bool, timeout: float | None) -> BatchEvaluationSummary`：批量评估（async）

#### mapper.py
职责：参数映射器
暴露接口：
- `MappingError`：映射错误异常
- `ParameterMapper`：参数映射器类
- `ParameterMapper.__init__(strict_mode: bool, enable_autoescape: bool)`：初始化参数映射器
- `ParameterMapper.map_inputs(template: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]`：将上下文映射到输入参数
- `ParameterMapper.map_outputs(template: dict[str, Any], result: dict[str, Any]) -> EvaluationResult`：将执行结果映射到 EvaluationResult
- `ContextBuilder`：上下文构建器类
- `ContextBuilder.build(task_goal: dict | None, criteria: dict | None, evidence: dict | None, metadata: dict | None) -> dict[str, Any]`：构建评估上下文
- `ContextBuilder.build_from_metric_config(metric_config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]`：从指标配置构建完整上下文

#### metric_loader.py
职责：评估指标文件加载器
暴露接口：
- `MetricLoader`：评估指标文件加载器类
- `MetricLoader.__init__(config_dir: str)`：初始化加载器，默认配置目录为 `config/evaluation_metrics`
- `MetricLoader.get_metric(metric_id: str) -> dict[str, Any] | None`：按 ID 获取评估指标（async）
- `MetricLoader.get_metric_by_name(name: str) -> dict[str, Any] | None`：按名称获取评估指标（async）
- `MetricLoader.get_metrics_by_ids(metric_ids: list[str]) -> list[dict[str, Any]]`：批量获取评估指标（async）
- `MetricLoader.list_metrics(category: str | None, status: str, limit: int, offset: int) -> list[dict[str, Any]]`：列出评估指标（async）
- `MetricLoader.reload() -> None`：重新加载配置文件
- `get_metric_loader() -> MetricLoader`：获取全局 MetricLoader 实例

### 测试策略
#### 模块测试
- 单元测试：评估结果模型、评分计算、参数映射
- 集成测试：工具评估、工作流评估、批量评估
- Mock 策略：Mock 工具执行器、工作流执行器

## 实现
→ 见代码文件
