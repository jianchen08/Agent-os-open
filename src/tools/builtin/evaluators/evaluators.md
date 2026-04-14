# 内置评估器组件

## 一、需求

### 1.1 组件职责

内置评估器组件提供任务执行结果的评估能力：
- 人工评估：创建审批请求，等待人工决策
- Schema 评估：验证输出数据格式

### 1.2 对外接口

- `HumanEvaluator`：人工评估器
- `SchemaEvaluator`：Schema 验证评估器

### 1.3 依赖

- `tools.builtin.base`：内置工具基类
- `tasks.services`：任务服务（审批流程）
- `core.logging`：日志模块

---

## 二、逻辑

### 2.1 流程设计

#### 人工评估流程

```
评估请求 → HumanEvaluator.execute()
              ↓
         创建审批请求
              ↓
         挂起任务等待
              ↓
         人工决策
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  通过      拒绝      需修改
    ↓         ↓         ↓
  继续执行  终止任务  返回修改
```

#### Schema 评估流程

```
数据验证 → SchemaEvaluator.execute()
              ↓
         加载 Schema
              ↓
         验证数据格式
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  通过      格式错误  缺失字段
    ↓         ↓         ↓
  返回成功  返回错误详情  返回缺失列表
```

### 2.2 数据流向

```
任务结果 → Evaluator
              ↓
    ┌─────────┴─────────┐
    ↓                   ↓
HumanEvaluator    SchemaEvaluator
    ↓                   ↓
审批服务           Schema验证
    ↓                   ↓
人工决策           验证结果
    ↓                   ↓
    └─────────┬─────────┘
              ↓
         评估结果
```

### 2.3 错误处理

- 审批超时：返回超时状态
- Schema 加载失败：返回配置错误
- 数据格式错误：返回详细错误信息

---

## 三、结构

### 3.1 子组件清单

无更深层子组件。

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `__init__.py` | 评估器导出 |
| `human_evaluator.py` | 人工评估器 |
| `schema_evaluator.py` | Schema 评估器 |

### 3.3 测试策略

- 单元测试：各评估器方法独立测试
- 集成测试：评估器与审批服务协作测试
- 覆盖率要求：核心逻辑 ≥85%

---

## 四、实现

### 4.1 human_evaluator.py

```
HumanEvaluator(BuiltinTool):
  execute(params: dict) -> ToolResult: 执行人工评估
  create_approval_request(task_id: str, context: dict) -> str: 创建审批请求
  wait_for_decision(request_id: str, timeout: int) -> ApprovalDecision: 等待审批决策
  process_decision(decision: ApprovalDecision) -> EvaluationResult: 处理决策结果
```

### 4.2 schema_evaluator.py

```
SchemaEvaluator(BuiltinTool):
  execute(params: dict) -> ToolResult: 执行 Schema 验证
  load_schema(schema_path: str) -> dict: 加载 Schema 定义
  validate_json(data: dict, schema: dict) -> ValidationResult: JSON 格式验证
  validate_yaml(data: dict, schema: dict) -> ValidationResult: YAML 格式验证
  format_errors(errors: List[ValidationError]) -> str: 格式化错误信息
```
