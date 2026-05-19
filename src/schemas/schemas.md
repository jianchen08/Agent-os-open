# 数据模式模块

## 一、需求

### 1.1 模块职责

提供 Pydantic 数据模式定义，用于 API 请求/响应验证和数据转换：
- 任务相关模式：任务创建、更新、响应
- 评估指标模式：指标创建、更新、响应
- 执行记录模式：记录创建、响应

### 1.2 对外接口

```python
# 任务模式
class TaskBase(BaseModel): ...
class TaskCreate(TaskBase): ...
class TaskUpdate(BaseModel): ...
class TaskCreateV2(BaseModel): ...
class TaskUpdateV2(BaseModel): ...
class TaskResponse(BaseModel): ...
class TaskDetailResponse(TaskResponse): ...

# 评估指标模式
class EvaluationMetricInfo(BaseModel): ...
class EvaluationMetricCreate(BaseModel): ...
class EvaluationMetricUpdate(BaseModel): ...
class EvaluationMetricResponse(BaseModel): ...

# 执行记录模式
class ExecutionRecordCreate(BaseModel): ...
class ExecutionRecordResponse(BaseModel): ...

# 评估状态模式
class EvaluationStatusResponse(BaseModel): ...
```

### 1.3 依赖

- `pydantic`：数据验证和序列化

---

## 二、逻辑

### 2.1 版本设计

模块提供两套模式以支持向后兼容：

| 版本 | 说明 |
|---|---|
| V1（旧版） | 使用 acceptance_criteria 字段 |
| V2（新版） | 使用 evaluation_metric_ids 字段，支持新的数据模型 |

### 2.2 数据模型

#### TaskBase（任务基础模式）
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| title | str | 是 | 任务标题 |
| agent_id | str | None | 关联的 Agent ID |
| priority | str | 否 | 任务优先级（默认 medium） |

#### TaskCreateV2（创建任务模式 V2）
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| title | str | 是 | 任务标题（1-255 字符） |
| agent_id | str | None | 执行者 ID |
| priority | str | 否 | 任务优先级：low | medium | high |
| goal | dict | None | 任务目标 |
| evaluation_metric_ids | list[str] | 否 | 评估指标 ID 列表 |
| parent_task_id | str | None | 父任务 ID |

#### TaskResponse（任务响应模式）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 任务 ID |
| parent_task_id | str | None | 父任务 ID |
| execution_record_id | str | None | 关联的执行记录 ID |
| user_id | str | None | 所属用户 |
| session_id | str | None | 来源会话 |
| title | str | 任务标题 |
| goal | dict | None | 任务目标 |
| target_type | str | None | 目标执行者类型 |
| target_id | str | None | 目标执行者 ID |
| target_name | str | None | 目标执行者名称 |
| priority | int | 优先级（1-10） |
| due_date | str | None | 截止日期 |
| retry_count | int | 重试次数 |
| max_retries | int | 最大重试次数 |
| evaluation_metric_ids | list[str] | None | 评估指标 ID 列表 |
| evaluation_metrics | list[EvaluationMetricInfo] | None | 评估指标详情 |
| status | str | 任务状态 |
| started_at | str | None | 开始时间 |
| completed_at | str | None | 完成时间 |
| created_at | str | 创建时间 |
| metadata | dict | None | 元数据 |
| subtasks | list[TaskResponse] | None | 子任务列表 |

#### EvaluationMetricCreate（创建评估指标模式）
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| name | str | 是 | 指标名称（唯一，1-100 字符） |
| description | str | 是 | 指标描述 |
| category | str | 是 | 指标分类：file | schema | test | code | api | performance | semantic | human |
| evaluator_type | str | 是 | 评估器类型：tool | agent | workflow | human |
| evaluator_id | str | 是 | 评估器 ID |
| default_config | dict | 否 | 默认配置 |
| input_schema | dict | 否 | 输入参数 Schema |
| when_to_use | list[str] | 否 | 适用场景列表 |
| when_not_to_use | list[str] | 否 | 不适用场景列表 |
| examples | list[dict] | 否 | 使用示例列表 |
| caveats | list[str] | 否 | 注意事项列表 |
| is_red_line | bool | 否 | 是否红线指标 |
| default_weight | float | 否 | 默认权重 |
| includes | list[str] | None | 包含的低级指标列表 |
| requires | list[str] | None | 前置依赖指标列表 |
| level | int | 否 | 指标层级 |
| source | str | 否 | 来源：builtin | generated | custom |
| status | str | 否 | 状态：active | inactive | deprecated |
| tags | list[str] | None | 标签 |

#### ExecutionRecordCreate（创建执行记录模式）
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| session_id | str | 是 | 会话 ID |
| parent_record_id | str | None | 父记录 ID |
| message_data | dict | 是 | 完整的消息数据 |

### 2.3 验证规则

- 字符串长度限制：title（1-255）、name（1-100）
- 枚举值验证：category、evaluator_type、status、source
- 递归模型：TaskResponse 的 subtasks 字段

---

## 三、结构

### 3.1 组件清单

| 组件 | 职责 |
|---|---|
| TaskBase | 任务基础模式 |
| TaskCreate | 创建任务模式（旧版） |
| TaskUpdate | 更新任务模式（旧版） |
| TaskCreateV2 | 创建任务模式（新版） |
| TaskUpdateV2 | 更新任务模式（新版） |
| TaskResponse | 任务响应模式 |
| TaskDetailResponse | 任务详情响应模式 |
| EvaluationMetricInfo | 评估指标信息模式 |
| EvaluationMetricCreate | 创建评估指标模式 |
| EvaluationMetricUpdate | 更新评估指标模式 |
| EvaluationMetricResponse | 评估指标响应模式 |
| ExecutionRecordCreate | 创建执行记录模式 |
| ExecutionRecordResponse | 执行记录响应模式 |
| EvaluationStatusResponse | 评估状态响应模式 |
| MessageType | 消息类型枚举（thinking/executing/waiting/completed/failed/cancelled） |
| MessageSubtype | 消息子类型枚举（text/error/progress/status/system） |
| UnifiedMessage | 统一消息模型（WebSocket + HTTP API 共用） |
| MESSAGE_TYPE_UI_MAP | 前端 UI 状态映射（颜色/图标/标签） |

### 3.2 文件清单

| 文件 | 说明 | 创建时间 |
|---|---|---|
| `task.py` | 任务相关模式定义 | — |
| `message.py` | 统一消息格式系统（枚举、模型、工具函数、UI映射） | 2026-05-15 |

### 3.3 测试策略

- 单元测试：验证各模式的字段验证规则
- 边界测试：测试字段长度限制、枚举值验证
- 序列化测试：测试 to_dict、model_dump 方法
