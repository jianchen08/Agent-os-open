# 任务服务组件

## 一、需求

### 1.1 组件职责

任务服务组件是任务模块的核心业务逻辑层，负责：
- 任务提交与编排
- 任务状态管理
- 任务评估与进度计算
- 任务恢复与续接
- 人工审批流程

### 1.2 对外接口

通过各 Service 类提供业务能力：
- `TaskSubmitOrchestrator`：任务提交编排入口
- `TaskStateService`：任务状态查询
- `TaskEvaluationAppService`：评估应用入口
- `TaskRecoveryService`：任务恢复操作
- `TaskContinuationService`：任务续接操作
- `TaskApprovalService`：人工审批操作

### 1.3 依赖

- `tasks.storage`：任务存储组件
- `tasks.models`：任务数据模型
- `core.logging`：日志模块
- `core.config`：配置模块

---

## 二、逻辑

### 2.1 流程设计

#### 任务提交流程

```
用户请求 → TaskSubmitOrchestrator
    ↓
TaskSubmissionService（创建任务）
    ↓
依赖验证 → 存储持久化
    ↓
返回任务ID
```

#### 任务评估流程

```
评估结果 → TaskEvaluationAppService
    ↓
EvaluationService（应用评估）
    ↓
进度重置机制 → 状态更新
    ↓
通知相关方
```

#### 任务恢复流程

```
恢复请求 → TaskRecoveryService
    ↓
状态检查 → 权限验证
    ↓
resume/cancel/retry 操作
    ↓
状态持久化
```

### 2.2 数据流向

```
外部请求 → Service层 → Storage层 → 数据库/文件
                ↓
           进度计算器
                ↓
           状态更新
```

### 2.3 错误处理

- 任务不存在：抛出 `TaskNotFoundError`
- 状态转换非法：抛出 `InvalidStateTransitionError`
- 依赖验证失败：抛出 `DependencyValidationError`
- 存储操作失败：抛出 `StorageError`

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| TaskSubmissionService | 任务创建与依赖验证 |
| TaskStateService | 状态查询与转换 |
| EvaluationService | 评估结果应用 |
| TaskRecoveryService | 任务恢复操作 |
| TaskContinuationService | 多轮续接 |
| TaskApprovalService | 人工审批 |
| ProgressCalculator | 统一进度计算 |
| TaskSubmitOrchestrator | 提交编排协调 |
| TaskEvaluationAppService | 评估应用入口 |
| TaskExecutionCallbackService | 执行回调处理 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `submission_service.py` | 任务提交服务 |
| `state_service.py` | 任务状态服务 |
| `evaluation_service.py` | 任务评估服务 |
| `recovery_service.py` | 任务恢复服务 |
| `continuation_service.py` | 任务续接服务 |
| `approval_service.py` | 人工审批服务 |
| `progress_calculator.py` | 进度计算器 |
| `task_submit_orchestrator.py` | 任务提交编排器 |
| `task_evaluation_app_service.py` | 评估应用服务 |
| `task_execution_callback_service.py` | 执行回调服务 |

### 3.3 测试策略

- 单元测试：各 Service 方法的独立测试
- 集成测试：Service 与 Storage 的协作测试
- 覆盖率要求：核心逻辑 ≥90%

---

## 四、实现

### 4.1 submission_service.py

```
TaskSubmissionService:
  submit_task(request: TaskSubmitRequest) -> Task: 创建并持久化任务
  validate_dependencies(task_id: str) -> bool: 验证任务依赖
```

### 4.2 state_service.py

```
TaskStateService:
  get_task(task_id: str) -> Task: 获取任务详情
  get_task_status(task_id: str) -> TaskStatus: 获取任务状态
  list_tasks(filter: TaskFilter) -> List[Task]: 查询任务列表
  update_status(task_id: str, status: TaskStatus) -> Task: 更新任务状态
```

### 4.3 evaluation_service.py

```
EvaluationService:
  apply_evaluation(task_id: str, result: EvaluationResult) -> Task: 应用评估结果
  reset_progress(task_id: str) -> None: 重置任务进度
```

### 4.4 recovery_service.py

```
TaskRecoveryService:
  resume_task(task_id: str) -> Task: 恢复暂停的任务
  cancel_task(task_id: str) -> Task: 取消任务
  retry_task(task_id: str) -> Task: 重试失败的任务
```

### 4.5 continuation_service.py

```
TaskContinuationService:
  continue_task(task_id: str, context: dict) -> Task: 续接任务执行
```

### 4.6 approval_service.py

```
TaskApprovalService:
  request_approval(task_id: str, approval_type: str) -> ApprovalRequest: 创建审批请求
  process_approval(request_id: str, decision: ApprovalDecision) -> Task: 处理审批决策
  get_pending_approvals(user_id: str) -> List[ApprovalRequest]: 获取待审批列表
```

### 4.7 progress_calculator.py

```
ProgressCalculator:
  calculate(task: Task) -> float: 计算任务进度
  calculate_project_progress(project_id: str) -> float: 计算项目进度
```

### 4.8 task_submit_orchestrator.py

```
TaskSubmitOrchestrator:
  submit(request: TaskSubmitRequest) -> Task: 编排任务提交流程
```

### 4.9 task_evaluation_app_service.py

```
TaskEvaluationAppService:
  apply(task_id: str, result: EvaluationResult) -> Task: 应用评估结果入口
```

### 4.10 task_execution_callback_service.py

```
TaskExecutionCallbackService:
  on_task_complete(task_id: str, result: Any) -> None: 任务完成回调
  on_task_failed(task_id: str, error: Exception) -> None: 任务失败回调
```
