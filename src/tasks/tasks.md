# 任务模块

## 需求

### 职责

提供任务执行闭环的核心功能，包括任务提交、评估执行、状态管理和进度追踪。

### 对外接口

- 输入：任务目标、验收标准、执行记录
- 输出：任务状态、评估结果、进度信息

### 依赖

- 外部依赖：SQLAlchemy、Pydantic
- 内部依赖： `src.db`（数据库模型）、`src.agents`（Agent 执行）、`src.tools`（工具执行）、`src.core.event_bus`（事件总线）

---

## 逻辑

### 流程设计

```
任务提交 → TaskSubmitOrchestrator → TaskSubmissionService → 创建任务记录 → 发布事件
任务执行 → TaskRunner → 加载 Agent → 执行 AgentLoop → 更新状态
评估执行 → EvaluationService.apply_result() → 更新 AC 状态 → 检查完成条件
状态流转 → StateMachine → pending → running → evaluating → completed/failed
```

### 数据流向

1. 任务提交：目标 + 验收标准 → Task 表 → 发布 TASK_SUBMITTED 事件
2. 任务执行：TaskRunner 订阅事件 → 加载 AgentConfig → 创建 AgentLoop → 执行
3. 评估执行：评估结果 → 更新 acceptance_criteria → 检查是否全部通过
4. 状态更新：状态变更 → 更新数据库 → 发布事件通知

### 数据模型

#### ExecutionStatus（执行状态）

> 状态定义来自 `core.states.ExecutionStatus`，统一管理所有执行状态。

| 状态 | 说明 |
|------|------|
| pending | 等待执行 |
| running | 执行中 |
| evaluating | 评估中 |
| completed | 已完成 |
| failed | 已失败 |
| blocked | 已阻塞 |
| cancelled | 已取消 |
| suspended | 已暂停 |
| scheduled | 已调度 |
| timeout | 已超时 |

#### TaskPhase（任务阶段）
| 阶段 | 说明 |
|------|------|
| preparation | 准备阶段 |
| execution | 执行阶段 |
| evaluation | 评估阶段 |
| completion | 完成阶段 |

#### EvaluationResult
| 字段 | 类型 | 说明 |
|------|------|------|
| passed | bool | 是否通过 |
| score | float | 得分 |
| feedback | str | 反馈 |
| details | dict | 详情 |
| issues | list[str] | 问题列表 |

#### EvaluationContext
| 字段 | 类型 | 说明 |
|------|------|------|
| task_id | str | 任务 ID |
| criteria_id | str | 验收标准 ID |
| evidence | dict | 证据 |

### API设计

#### 模块级接口
| 接口 | 说明 |
|------|------|
| `TaskManager` | 任务管理器 |
| `TaskRunner` | 任务执行器 |
| `ACEvaluator` | 验收标准评估器 |
| `TimerManager` | 计时器管理器 |

> 注意：AutoExecuteWatchdog 已移除，请使用统一决策引擎 `src.agents.decision`
> 注意：TaskPhaseController 已移除，任务状态由 should_continue 机制管理

### 配置设计

#### 模块配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| DEFAULT_MAX_RETRIES | 默认最大重试次数 | 3 |
| AC_MAX_RETRIES | 验收标准最大重试次数 | 5 |
| MAX_RETRIES | 执行器最大重试次数 | 3 |

### 错误处理

#### 模块错误码
| 错误类型 | 说明 |
|----------|------|
| TASK_NOT_FOUND | 任务不存在 |
| TASK_COMPLETED | 任务已完成 |
| TASK_BLOCKED | 任务已阻塞 |
| CRITERIA_NOT_FOUND | 验收标准不存在 |
| AC_MAX_RETRIES_EXCEEDED | 验收标准重试次数超限 |

### 安全设计

#### 模块安全
- 执行隔离：每个任务使用独立数据库会话
- 去重机制：防止同一任务重复执行
- 重试限制：失败任务最多重试 3 次
- 事件驱动：避免回调嵌套导致的资源泄漏

---

## 结构

### 组件清单（文件夹 - 抽象说明）

| 组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| services | 任务服务层 | 输入：业务请求 → 输出：业务结果 | [services.md](services/services.md) |
| storage | 任务存储层 | 输入：数据操作 → 输出：持久化结果 | - |

### 文件清单（代码文件 - 具体接口）

#### services/submission/submission_service.py
职责：任务提交服务
暴露接口：
- `TaskSubmissionService`：任务提交服务类
  - `submit(...) -> dict`：提交任务
  - `_validate_dependencies(...) -> dict`：验证依赖关系
  - `_resolve_metrics(...) -> dict`：解析评估指标
  - `_create_execution_record(...) -> str`：创建执行记录
  - `_publish_submitted_event(...) -> None`：发布提交事件

#### services/submission/submit_orchestrator.py
职责：任务提交编排器
暴露接口：
- `TaskSubmitOrchestrator`：任务提交编排器类
  - `submit_to_agent(...) -> ToolExecutionResult`：提交任务给 Agent
  - `submit_to_workflow(...) -> ToolExecutionResult`：提交任务给工作流

#### services/execution/runner.py
职责：任务执行器（核心）
暴露接口：
- `TaskRunner`：任务执行器类
  - `start() -> None`：启动执行器
  - `stop() -> None`：停止执行器
  - `execute_task(task_id: str) -> None`：执行任务
  - `execute_task_with_result(task_id: str) -> dict`：执行任务并返回结果

#### services/execution/loader.py
职责：任务加载器
暴露接口：
- `TaskLoader`：任务加载器类
  - `load_task(task_id: str) -> Task`：加载任务
  - `check_task_exists(task_id: str) -> bool`：检查任务是否存在
  - `load_agent_config(agent_id: str) -> AgentConfig`：加载 Agent 配置

#### services/execution/state_manager.py
职责：状态管理器
暴露接口：
- `TaskStateManager`：状态管理器类
  - `update_task_status(...) -> None`：更新任务状态
  - `send_execution_start(...) -> None`：发送执行开始事件
  - `send_execution_done(...) -> None`：发送执行完成事件

#### services/execution/input_builder.py
职责：输入构建器
暴露接口：
- `TaskInputBuilder`：输入构建器类
  - `build_user_input(task, agent_config) -> str`：构建用户输入

#### services/execution/agent_executor.py
职责：Agent 执行
暴露接口：
- `execute_agent_loop(...) -> AgentResult`：执行 Agent

#### services/execution/workflow_executor.py
职责：Workflow 执行
暴露接口：
- `execute_workflow(...) -> dict`：执行工作流

#### state_machine.py
职责：任务状态机
暴露接口：
- `ExecutionStatus`：执行状态枚举（来自 `core.states`）
- `TaskStateMachine`：任务状态机类

#### timer_manager.py
职责：计时器管理器
暴露接口：
- `TimerStatus`：计时器状态枚举（active, expired, cancelled）
- `TimerState`：计时器状态数据类
- `TimerManager`：计时器管理器类

#### progress.py
职责：进度追踪
暴露接口：
- `TaskProgress`：任务进度类

#### dependency_validator.py
职责：依赖验证器
暴露接口：
- `DependencyValidator`：依赖验证器类

#### recovery_orchestrator.py
职责：恢复编排器
暴露接口：
- `RecoveryOrchestrator`：恢复编排器类

#### services/state_service.py
职责：状态服务
暴露接口：
- `TaskStateService`：状态服务类

#### services/evaluation_service.py
职责：评估服务
暴露接口：
- `EvaluationService`：评估服务类

#### services/recovery_service.py
职责：恢复服务（已移除）
> 注意：TaskRecoveryService 已移除，请使用 `src.orchestration.recovery.TaskRecoveryOrchestrator`

#### services/approval_service.py
职责：审批服务
暴露接口：
- `ApprovalService`：审批服务类

#### storage/db_storage.py
职责：数据库存储
暴露接口：
- `TaskDBStorage`：任务数据库存储类

### 测试策略

#### 模块测试
- 单元测试：状态机、阶段控制器、评估器
- 集成测试：任务提交、执行、评估流程
- 覆盖率要求：核心逻辑 ≥ 85%

---

## 实现

→ 见各组件代码文件
