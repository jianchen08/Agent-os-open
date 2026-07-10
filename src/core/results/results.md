# 统一执行结果类型

## 需求

### 职责

提供统一的执行结果类型定义，用于 Tool、Agent、Workflow、Evaluation 等执行实体的结果表示。所有执行结果继承自 `ExecutionResult` 基类，确保：

- 统一的状态表示（使用 `ExecutionStatus` 枚举）
- 统一的时间追踪
- 统一的错误处理
- 统一的序列化方法

### 对外接口

- 输入：执行状态、输出数据、错误信息、时间信息
- 输出：结构化的执行结果对象

### 依赖

- `src/core/states`：ExecutionStatus 执行状态枚举
- `pydantic`：BaseModel 基类

## 逻辑

### 类图关系

```
ExecutionResult (基类, Generic[T])
├── AgentExecutionResult (ExecutionResult[str])
│   └── 特有字段: iterations, tool_calls, reasoning, agent_id, agent_name
├── ToolExecutionResult (ExecutionResult[Any])
│   └── 特有字段: tool_name, tool_id, input_params
├── WorkflowExecutionResult (ExecutionResult[dict])
│   └── 特有字段: workflow_id, workflow_version, progress, inputs, node_executions
└── EvaluationExecutionResult (ExecutionResult[Any])
    └── 特有字段: metric_id, passed, score, weight, evidence, suggestions

辅助类型:
├── ToolCallRecord: 工具调用记录
└── EvaluationStatus: 评估状态枚举（独立于 ExecutionStatus）
```

### 状态类型关系

#### ExecutionStatus 与 EvaluationStatus 的区别

| 状态类型 | 维度 | 用途 | 定义位置 |
|----------|------|------|----------|
| **ExecutionStatus** | 执行维度 | 表示执行实体的生命周期状态 | `core/states` |
| **EvaluationStatus** | 评估维度 | 表示评估结果的通过/未通过状态 | `core/results/evaluation.py` |

#### 状态流转

```
ExecutionStatus（执行维度）:
PENDING → SCHEDULED → RUNNING → EVALUATING → COMPLETED | FAILED | CANCELLED | TIMEOUT
                          ↓              ↓
                      SUSPENDED      BLOCKED

EvaluationStatus（评估维度）:
PENDING → EVALUATING → PASSED | FAILED | ERROR | TIMEOUT
```

### 数据模型

#### ExecutionResult（执行结果基类）

| 字段 | 类型 | 说明 |
|------|------|------|
| status | ExecutionStatus | 执行状态 |
| output | T \| None | 输出数据（泛型） |
| error | str \| None | 错误信息 |
| error_code | str \| None | 错误代码 |
| started_at | datetime \| None | 开始时间 |
| completed_at | datetime \| None | 完成时间 |
| duration_ms | int \| None | 执行时长（毫秒） |
| metadata | dict[str, Any] | 扩展元数据 |

#### AgentExecutionResult（Agent 执行结果）

| 字段 | 类型 | 说明 |
|------|------|------|
| 继承基类所有字段 | - | - |
| iterations | int | 迭代次数 |
| tool_calls | list[ToolCallRecord] | 工具调用记录 |
| reasoning | str \| None | 推理过程（思考模式） |
| agent_id | str \| None | Agent ID |
| agent_name | str \| None | Agent 名称 |

#### ToolExecutionResult（工具执行结果）

| 字段 | 类型 | 说明 |
|------|------|------|
| 继承基类所有字段 | - | - |
| tool_name | str \| None | 工具名称 |
| tool_id | str \| None | 工具 ID |
| input_params | dict[str, Any] | 输入参数 |

#### WorkflowExecutionResult（工作流执行结果）

| 字段 | 类型 | 说明 |
|------|------|------|
| 继承基类所有字段 | - | - |
| workflow_id | str | 工作流 ID |
| workflow_version | str | 工作流版本 |
| progress | float | 执行进度 (0.0-1.0) |
| inputs | dict[str, Any] | 输入参数 |
| node_executions | list[Any] | 节点执行记录 |

#### EvaluationExecutionResult（评估执行结果）

| 字段 | 类型 | 说明 |
|------|------|------|
| status | EvaluationStatus | 评估状态（覆盖基类） |
| metric_id | str | 指标 ID |
| metric_name | str | 指标名称 |
| passed | bool | 是否通过 |
| score | float | 评分 (0-100) |
| weight | float | 权重 |
| is_red_line | bool | 是否为红线指标 |
| evidence | list[str] | 证据列表 |
| suggestions | list[str] | 改进建议 |
| evaluator_type | str | 评估器类型 |
| evaluator_id | str | 评估器 ID |

#### ToolCallRecord（工具调用记录）

| 字段 | 类型 | 说明 |
|------|------|------|
| tool_name | str | 工具名称 |
| inputs | dict[str, Any] | 输入参数 |
| output | Any \| None | 输出结果 |
| success | bool | 是否成功 |
| error | str \| None | 错误信息 |
| duration_ms | int | 执行时间（毫秒） |

#### EvaluationStatus（评估状态枚举）

| 值 | 说明 |
|----|------|
| PENDING | 待评估 |
| EVALUATING | 评估中 |
| PASSED | 已通过 |
| FAILED | 未通过 |
| TIMEOUT | 超时 |
| ERROR | 错误 |

### API 设计

#### ExecutionResult 工厂方法

| 方法 | 职责 |
|------|------|
| `create_running(**kwargs) -> ExecutionResult[T]` | 创建运行中状态的结果 |
| `create_completed(output: T, **kwargs) -> ExecutionResult[T]` | 创建成功完成的结果 |
| `create_failed(error: str, error_code: str \| None, **kwargs) -> ExecutionResult[T]` | 创建失败结果 |

#### ExecutionResult 便捷属性

| 属性 | 说明 |
|------|------|
| `success` | 是否成功完成（status == COMPLETED） |
| `is_failed` | 是否失败（FAILED/TIMEOUT/CANCELLED） |
| `is_terminal` | 是否已终止（终态） |

#### ExecutionResult 序列化方法

| 方法 | 职责 |
|------|------|
| `to_dict() -> dict[str, Any]` | 转换为字典（统一序列化） |
| `calculate_duration() -> int \| None` | 计算执行时长 |

#### AgentExecutionResult 便捷属性

| 属性 | 说明 |
|------|------|
| `total_tool_calls` | 工具调用总次数 |
| `successful_tool_calls` | 成功的工具调用次数 |

#### WorkflowExecutionResult 方法

| 方法 | 职责 |
|------|------|
| `to_summary() -> dict[str, Any]` | 转换为摘要信息（用于列表显示） |
| `update_progress() -> None` | 更新执行进度 |

#### EvaluationExecutionResult 方法

| 方法 | 职责 |
|------|------|
| `from_legacy_format(data: dict, metric_id: str, metric_name: str) -> EvaluationExecutionResult` | 从旧格式创建（兼容 0-1 分数） |
| `from_dict(data: dict, ...) -> EvaluationExecutionResult` | 从字典创建 |
| `is_success() -> bool` | 检查评估是否成功 |
| `has_error() -> bool` | 检查评估是否有错误 |

### 错误处理

通过 `error` 和 `error_code` 字段统一处理错误信息，无需额外异常类型。

### 安全设计

- 使用 Pydantic 进行数据验证
- 敏感信息不应存储在 metadata 中

## 结构

### 组件清单

| 组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| base.py | 执行结果基类 | ExecutionResult | - |
| agent.py | Agent 执行结果 | AgentExecutionResult | - |
| tool.py | 工具执行结果 | ToolExecutionResult | - |
| workflow.py | 工作流执行结果 | WorkflowExecutionResult | - |
| evaluation.py | 评估执行结果 | EvaluationExecutionResult, EvaluationStatus | - |
| tool_call.py | 工具调用记录 | ToolCallRecord | - |

### 文件清单

#### base.py

职责：执行结果基类定义

暴露接口：
- `ExecutionResult`：执行结果基类（泛型）
  - `status: ExecutionStatus`：执行状态
  - `output: T | None`：输出数据
  - `error: str | None`：错误信息
  - `error_code: str | None`：错误代码
  - `started_at: datetime | None`：开始时间
  - `completed_at: datetime | None`：完成时间
  - `duration_ms: int | None`：执行时长（毫秒）
  - `metadata: dict[str, Any]`：扩展元数据
  - `@property success -> bool`：是否成功完成
  - `@property is_failed -> bool`：是否失败
  - `@property is_terminal -> bool`：是否已终止
  - `create_running(**kwargs) -> ExecutionResult[T]`：创建运行中状态
  - `create_completed(output: T, **kwargs) -> ExecutionResult[T]`：创建成功完成
  - `create_failed(error: str, error_code: str | None, **kwargs) -> ExecutionResult[T]`：创建失败
  - `to_dict() -> dict[str, Any]`：转换为字典
  - `calculate_duration() -> int | None`：计算执行时长

#### agent.py

职责：Agent 执行结果定义

暴露接口：
- `AgentExecutionResult`：Agent 执行结果类
  - 继承 `ExecutionResult[str]`
  - `iterations: int`：迭代次数
  - `tool_calls: list[ToolCallRecord]`：工具调用记录
  - `reasoning: str | None`：推理过程
  - `agent_id: str | None`：Agent ID
  - `agent_name: str | None`：Agent 名称
  - `@property total_tool_calls -> int`：工具调用总次数
  - `@property successful_tool_calls -> int`：成功的工具调用次数
  - `to_dict() -> dict[str, Any]`：转换为字典

#### tool.py

职责：工具执行结果定义

暴露接口：
- `ToolExecutionResult`：工具执行结果类
  - 继承 `ExecutionResult[Any]`
  - `tool_name: str | None`：工具名称
  - `tool_id: str | None`：工具 ID
  - `input_params: dict[str, Any]`：输入参数
  - `@property data -> Any | None`：兼容旧字段名（等同于 output）
  - `@property duration -> float`：兼容旧字段名（秒）
  - `@property result -> Any | None`：兼容旧字段名（已废弃）
  - `to_dict() -> dict[str, Any]`：转换为字典

#### workflow.py

职责：工作流执行结果定义

暴露接口：
- `WorkflowExecutionResult`：工作流执行结果类
  - 继承 `ExecutionResult[dict[str, Any]]`
  - `workflow_id: str`：工作流 ID
  - `workflow_version: str`：工作流版本
  - `progress: float`：执行进度
  - `inputs: dict[str, Any]`：输入参数
  - `node_executions: list[Any]`：节点执行记录
  - `to_dict() -> dict[str, Any]`：转换为字典
  - `to_summary() -> dict[str, Any]`：转换为摘要信息
  - `update_progress() -> None`：更新执行进度

#### evaluation.py

职责：评估执行结果定义

暴露接口：
- `EvaluationStatus`：评估状态枚举
  - `PENDING`：待评估
  - `EVALUATING`：评估中
  - `PASSED`：已通过
  - `FAILED`：未通过
  - `TIMEOUT`：超时
  - `ERROR`：错误
- `EvaluationExecutionResult`：评估执行结果类
  - 继承 `ExecutionResult[Any]`
  - `status: EvaluationStatus`：评估状态（覆盖基类）
  - `metric_id: str`：指标 ID
  - `metric_name: str`：指标名称
  - `passed: bool`：是否通过
  - `score: float`：评分 (0-100)
  - `weight: float`：权重
  - `is_red_line: bool`：是否红线指标
  - `evidence: list[str]`：证据列表
  - `suggestions: list[str]`：改进建议
  - `evaluator_type: str`：评估器类型
  - `evaluator_id: str`：评估器 ID
  - `@property success -> bool`：是否成功（覆盖基类，等同于 passed）
  - `@property message -> str`：兼容旧字段名
  - `@property execution_time_ms -> float | None`：兼容旧字段名
  - `@property details -> dict[str, Any]`：兼容旧字段名
  - `is_success() -> bool`：检查评估是否成功
  - `has_error() -> bool`：检查评估是否有错误
  - `to_dict() -> dict[str, Any]`：转换为字典
  - `from_legacy_format(data: dict, ...) -> EvaluationExecutionResult`：从旧格式创建
  - `from_dict(data: dict, ...) -> EvaluationExecutionResult`：从字典创建

#### tool_call.py

职责：工具调用记录定义

暴露接口：
- `ToolCallRecord`：工具调用记录类
  - `tool_name: str`：工具名称
  - `inputs: dict[str, Any]`：输入参数
  - `output: Any | None`：输出结果
  - `success: bool`：是否成功
  - `error: str | None`：错误信息
  - `duration_ms: int`：执行时间（毫秒）

### 测试策略

- 单元测试：工厂方法、序列化方法、便捷属性
- 集成测试：与其他模块的集成
- 测试覆盖：核心逻辑 ≥90%

## 迁移指南

### 从旧类型迁移

| 旧类型 | 新类型 | 说明 |
|--------|--------|------|
| AgentResult | AgentExecutionResult | 通过别名兼容 |
| ToolResult | ToolExecutionResult | 通过别名兼容 |
| WorkflowExecution.outputs | WorkflowExecutionResult.output | 字段名变更 |
| EvaluationResult | EvaluationExecutionResult | 通过别名兼容 |

### 字段映射

| 旧字段 | 新字段 | 说明 |
|--------|--------|------|
| success | status == COMPLETED | 使用状态枚举 |
| data | output | 统一命名 |
| duration (秒) | duration_ms (毫秒) | 统一单位 |
| message | output | 评估结果专用 |
| execution_time_ms | duration_ms | 统一命名 |

### 兼容性属性

各子类提供了兼容性属性以支持平滑迁移：

**ToolExecutionResult**:
- `data` → `output`
- `duration` → `duration_ms / 1000`
- `result` → `output`（已废弃）

**EvaluationExecutionResult**:
- `message` → `output`（字符串）
- `execution_time_ms` → `duration_ms`
- `details` → `metadata`

## 实现

→ 见各组件代码文件
