# 评估系统 (evaluation)

> M5b 里程碑交付 — 统一评估引擎，支持 5 类评估指标

## 一、需求

任务完成后需要自动评估其产出质量。评估指标分为三类：

| 类型 | 指标 | 评估方式 |
|------|------|---------|
| **tool** | file_check, format_valid, bash_check | 调用工具执行检查，通过 expect 条件判定 |
| **agent** | semantic_check | 调用独立 LLM Agent 评估，返回 passed/score/feedback |
| **human** | human_review | 等待人工审核，返回 approved/rejected |

评估结果需映射到任务状态：`evaluating → completed`（通过）或 `evaluating → failed`（不通过）。

## 二、逻辑

### 评估流程

```
任务完成
  → TaskService.move_to_evaluating()
  → EvaluationExecutor.run_evaluation()
      → MetricLoader.get() 获取指标定义
      → EvaluationEngine._evaluate_metric()
          → 按 metric_type 分发到评估器
          → tool: _evaluate_tool() (Mock, 后续接入 ToolRegistry)
          → agent: _evaluate_agent() (Mock, 后续接入 LLM Agent)
          → human: _evaluate_human() (Mock, 后续接入 human_interaction)
          → 评估器返回 output dict
          → ExpectEvaluator.evaluate() 判定期望条件
      → ResultMapper.map_to_task_status() 映射 pass/fail
  → TaskService.complete_evaluation(passed)
```

### 期望条件判定

支持的操作符：`is_true`, `is_false`, `equals`, `not_equals`, `in`, `not_in`, `contains`, `gt`, `lt`, `gte`, `lte`

字段路径支持嵌套：`data.exit_code` → `output["data"]["exit_code"]`

组合逻辑：`and`（全部满足）/ `or`（任一满足）

### 综合判定

当前简化版：所有指标通过 → 整体通过。后续可引入权重和红线机制。

## 三、结构

### 文件清单

| 文件 | 行数 | 职责 |
|------|------|------|
| `__init__.py` | ~30 | 模块导出（公共 API） |
| `types.py` | ~120 | MetricType, MetricResult, EvaluationConfig, MetricDefinition, ExpectSpec, ExpectCondition |
| `engine.py` | ~200 | EvaluationEngine — 统一评估引擎（分发 + Mock 评估器） |
| `expect.py` | ~130 | ExpectEvaluator — 期望条件判定（11 种操作符 + 嵌套字段） |
| `mapper.py` | ~80 | ResultMapper — 评估结果 → 任务状态映射 |
| `loader.py` | ~160 | MetricLoader — YAML 指标文件加载/解析 |
| `executor.py` | ~100 | EvaluationExecutor — 执行评估 + TaskService 回写 |
| `README.md` | 本文件 | 模块文档 |

### 配置文件

5 个评估指标 YAML 位于 `config/evaluation_metrics/`：

| 文件 | 类型 | evaluator_id |
|------|------|-------------|
| file_check.yaml | tool | file_read |
| format_valid.yaml | tool | schema_evaluator |
| bash_check.yaml | tool | bash_execute |
| semantic_check.yaml | agent | evaluator_agent |
| human_review.yaml | human | human_interaction |

### 测试

`tests/test_evaluation.py` — 34 个测试用例，覆盖：
- TestMetricLoader (8): 加载/解析/查询
- TestExpectEvaluator (8): 条件判定/操作符/嵌套字段
- TestEvaluationEngine (8): 分发/Mock/fail_fast/自定义评估器
- TestResultMapper (4): 映射/摘要
- TestEvaluationExecutor (3): 执行/TaskService 集成
- TestEvaluationResult (3): 综合判定

### 公共 API

```python
from evaluation import (
    MetricType,           # 指标类型枚举
    MetricResult,         # 单个指标评估结果
    EvaluationResult,     # 完整评估结果
    EvaluationConfig,     # 评估配置
    MetricDefinition,     # 指标定义
    MetricLoader,         # YAML 指标加载器
    EvaluationEngine,     # 统一评估引擎
    ExpectEvaluator,      # 期望值评估器
    ResultMapper,         # 结果映射器
    EvaluationExecutor,   # 评估执行器
)
```

### 使用示例

```python
from evaluation import EvaluationExecutor

# 创建执行器（注入 TaskService）
executor = EvaluationExecutor(task_service=task_service)

# 运行评估
result = executor.run_evaluation(
    task_id="abc123",
    metric_ids=["file_check", "format_valid"],
)

# 查看结果
print(result.overall_passed)  # True/False
print(result.summary)          # "2/2 指标通过"
```
