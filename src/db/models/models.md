# 模型组件

## 需求
### 职责
定义数据库 ORM 模型，基于 SQLAlchemy 2.0 提供异步数据访问能力。

### 对外接口
- 输入：无（模型定义）
- 输出：ORM 模型类

### 依赖
- 外部依赖：sqlalchemy
- 内部依赖：无

## 逻辑
### 流程设计
模型定义 -> SQLAlchemy 映射 -> 数据库表

### 数据模型
#### 模型清单
| 模型 | 表名 | 职责 |
|---|---|---|
| User | users | 用户信息 |
| Session | sessions | 会话信息 |
| ExecutionRecord | execution_records | 执行记录 |
| AgentConfig | agent_configs | Agent 配置 |
| Task | tasks | 任务定义 |
| EvaluationMetric | evaluation_metrics | 评估指标 |
| Workflow | workflows | 工作流定义 |
| ToolLibrary | tool_libraries | 工具库 |
| EpisodesMemory | episodes_memory | 情景记忆 |
| SemanticMemory | semantic_memory | 语义记忆 |
| KnowledgeBase | knowledge_bases | 知识库 |
| Notification | notifications | 通知 |
| Trigger | triggers | 触发器 |
| UsageRecord | usage_records | 用量记录 |

#### Task 模型（核心）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(42) | 任务 ID（主键） |
| parent_task_id | String(42) | 父任务 ID |
| execution_record_id | String(42) | 执行记录 ID |
| user_id | String(36) | 用户 ID |
| session_id | String(36) | 会话 ID |
| title | String(255) | 任务标题 |
| goal | JSON | 任务目标 |
| target_type | String(50) | 目标类型 |
| status | String(50) | 任务状态 |
| priority | Integer | 优先级 |
| dependencies | JSON | 依赖任务列表 |
| evaluation_metric_ids | JSON | 评估指标 ID 列表 |
| task_metadata | JSON | 元数据 |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |

#### User 模型
| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(36) | 用户 ID（主键） |
| username | String(255) | 用户名（唯一） |
| email_encrypted | LargeBinary | 加密邮箱 |
| password_hash | Text | 密码哈希 |
| role | String(50) | 角色 |
| preferences | JSON | 偏好设置 |
| is_active | Boolean | 是否激活 |

#### Session 模型
| 字段 | 类型 | 说明 |
|---|---|---|
| user_id | String(36) | 用户 ID（复合主键） |
| session_seq | Integer | 会话序号（复合主键） |
| id | String(50) | 全局唯一 ID |
| agent_id | String(255) | 绑定的 Agent ID |
| title | String(255) | 会话标题 |
| status | String(50) | 会话状态 |

## 结构
### 子组件清单
无

### 文件清单（代码文件 - 具体接口）
#### base.py
职责：模型基类
暴露接口：
- `Base`：SQLAlchemy 声明基类

#### user.py
职责：用户和会话模型
暴露接口：
- `User`：用户模型类
- `Session`：会话模型类

#### task.py
职责：任务和评估指标模型
暴露接口：
- `Task`：任务模型类
  - `description -> str | None`：描述属性
  - `acceptance_criteria -> list`：验收标准属性
  - `progress_percent -> float`：进度百分比属性
- `EvaluationMetric`：评估指标模型类

#### execution.py
职责：执行记录模型
暴露接口：
- `ExecutionRecord`：执行记录模型类

#### agent.py
职责：Agent 配置模型
暴露接口：
- `AgentConfig`：Agent 配置模型类

#### workflow.py
职责：工作流模型
暴露接口：
- `Workflow`：工作流模型类
- `WorkflowComposition`：工作流组合模型类

#### tool.py
职责：工具库模型
暴露接口：
- `ToolLibrary`：工具库模型类

#### memory.py
职责：记忆系统模型
暴露接口：
- `EpisodesMemory`：情景记忆模型
- `SemanticMemory`：语义记忆模型
- `KnowledgeBase`：知识库模型
- `Tag`：标签模型
- `MemoryTag`：记忆标签关联模型
- `MemoryChunk`：记忆块模型
- `TagCooccurrence`：标签共现模型

#### monitoring.py
职责：监控和用量统计模型
暴露接口：
- `MonitoringAlert`：监控告警模型
- `TaskQueueStats`：任务队列统计模型
- `UsageRecord`：用量记录模型
- `UsageStatistics`：用量统计模型

#### trigger.py
职责：触发器系统模型
暴露接口：
- `Trigger`：触发器模型
- `TriggerAction`：触发器动作模型
- `TriggerExecutionLog`：触发器执行日志模型

#### notification.py
职责：通知系统模型
暴露接口：
- `Notification`：通知模型类

#### experience.py
职责：执行单元和经验模型
暴露接口：
- `ExecutionUnit`：执行单元模型
- `ExecutionExperience`：执行经验模型
- `AgentCallRecord`：Agent 调用记录模型

#### rollback.py
职责：回滚机制模型
暴露接口：
- `RollbackCheckpoint`：回滚检查点模型
- `RollbackOperationLog`：回滚操作日志模型

#### __init__.py
职责：模型模块导出
暴露接口：
- 导出所有模型类
- `Agent = AgentConfig`：兼容别名

### 测试策略
#### 组件测试
- 单元测试：模型字段验证、关系映射
- 集成测试：CRUD 操作
- 覆盖率要求：模型定义 >= 70%

## 实现
-> 见代码文件：src/db/models/
