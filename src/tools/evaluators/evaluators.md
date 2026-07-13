# 工具评估器组件

## 一、需求

### 1.1 组件职责

工具评估器组件负责评估结果的存储、传播与查询：
- 评估结果持久化存储
- 结果传播机制（回调通知）
- 评估结果查询 API
- 评估结果包装与汇总

### 1.2 对外接口

- `EvaluationResultStorage`：评估结果存储
- `ResultPropagator`：结果传播器
- `EvaluationResultQuery`：结果查询
- `ResultWrapper`：结果包装器

### 1.3 依赖

- `core.database`：数据库连接
- `core.logging`：日志模块
- `tasks.services`：任务服务

---

## 二、逻辑

### 2.1 流程设计

#### 评估结果存储流程

```
评估结果 → EvaluationResultStorage
              ↓
         验证结果格式
              ↓
         持久化存储
              ↓
         触发传播
              ↓
         返回存储ID
```

#### 结果传播流程

```
存储完成 → ResultPropagator
              ↓
         获取订阅者列表
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  回调通知  事件发布  状态更新
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         传播完成确认
```

#### 结果查询流程

```
查询请求 → EvaluationResultQuery
              ↓
         解析查询条件
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  按任务ID  按时间范围  按评估类型
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         返回结果列表
```

### 2.2 数据流向

```
工具执行结果 → ResultWrapper
                   ↓
              包装与汇总
                   ↓
           EvaluationResultStorage
                   ↓
              持久化存储
                   ↓
           ResultPropagator
                   ↓
    ┌──────────────┼──────────────┐
    ↓              ↓              ↓
回调服务       事件总线       任务服务
```

### 2.3 数据模型

#### EvaluationResult

| 字段 | 类型 | 说明 |
|------|------|------|
| id | str | 结果唯一标识 |
| task_id | str | 关联任务ID |
| evaluator | str | 评估器类型 |
| result | str | 评估结果（pass/fail/pending） |
| details | dict | 详细信息 |
| created_at | datetime | 创建时间 |

#### EvaluationSummary

| 字段 | 类型 | 说明 |
|------|------|------|
| total | int | 总评估数 |
| passed | int | 通过数 |
| failed | int | 失败数 |
| pending | int | 待处理数 |

### 2.4 错误处理

- 存储失败：记录日志并重试
- 传播失败：标记为待重传
- 查询超时：返回部分结果

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| EvaluationResultStorage | 评估结果持久化 |
| ResultPropagator | 结果传播与通知 |
| EvaluationResultQuery | 结果查询 API |
| ResultWrapper | 结果包装与汇总 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `__init__.py` | 模块导出 |
| `result_storage.py` | 评估结果存储 |
| `result_propagator.py` | 结果传播器 |
| `result_query.py` | 结果查询 |
| `result_wrapper.py` | 结果包装器 |

### 3.3 测试策略

- 单元测试：各组件方法独立测试
- 集成测试：存储与传播协作测试
- 覆盖率要求：核心逻辑 ≥85%

---

## 四、实现

### 4.1 result_storage.py

```
EvaluationResultStorage:
  store(result: EvaluationResult) -> str: 存储评估结果
  get(result_id: str) -> EvaluationResult: 获取评估结果
  get_by_task(task_id: str) -> List[EvaluationResult]: 按任务查询
  update(result_id: str, updates: dict) -> EvaluationResult: 更新结果
```

### 4.2 result_propagator.py

```
ResultPropagator:
  propagate(result: EvaluationResult) -> None: 传播评估结果
  register_callback(event: str, callback: Callable) -> None: 注册回调
  notify_subscribers(result: EvaluationResult) -> None: 通知订阅者
```

### 4.3 result_query.py

```
EvaluationResultQuery:
  query_by_task(task_id: str) -> List[EvaluationResult]: 按任务查询
  query_by_time(start: datetime, end: datetime) -> List[EvaluationResult]: 按时间查询
  query_by_type(evaluator: str) -> List[EvaluationResult]: 按类型查询
  get_summary(task_id: str) -> EvaluationSummary: 获取汇总统计
```

### 4.4 result_wrapper.py

```
EvaluationResult(DataClass):
  id: str
  task_id: str
  evaluator: str
  result: str
  details: dict
  created_at: datetime

EvaluationSummary(DataClass):
  total: int
  passed: int
  failed: int
  pending: int

ResultWrapper:
  wrap(raw_result: dict) -> EvaluationResult: 包装原始结果
  summarize(results: List[EvaluationResult]) -> EvaluationSummary: 汇总结果
```
