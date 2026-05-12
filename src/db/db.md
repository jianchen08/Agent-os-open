# DB 模块

## 需求

### 职责

数据库模块负责管理数据库连接、ORM 模型定义和数据访问，提供统一的数据库操作接口。基于 SQLAlchemy 2.0 异步实现。

### 对外接口

- 输入：数据库操作请求
- 输出：数据库会话 / 模型实例

### 依赖

- 外部依赖：sqlalchemy、aiosqlite/asyncpg

## 逻辑

### 流程设计

```
应用启动
    │
    ▼
DatabaseManager 初始化
    │
    ├─► 创建异步引擎（AsyncEngine）
    │
    └─► 创建会话工厂（async_sessionmaker）
    │
    ▼
请求到达
    │
    ▼
get_async_session() 获取会话
    │
    ▼
执行数据库操作
    │
    ▼
提交/回滚事务
    │
    ▼
关闭会话
```

### 数据流向

```
API 请求 → get_async_session()
    ↓
会话创建 → 业务操作
    ↓
模型操作 → 数据库
    ↓
事务提交 → 返回结果
```

### 数据模型

#### 核心模型清单

| 模型 | 说明 | 表名 |
|------|------|------|
| User | 用户 | users |
| Session | 会话 | sessions |
| ExecutionRecord | 执行记录 | execution_records |
| AgentConfig | Agent 配置 | agent_configs |
| Task | 任务 | tasks |
| EvaluationMetric | 评估指标 | evaluation_metrics |
| Workflow | 工作流 | workflows |
| ToolLibrary | 工具库 | tool_libraries |
| EpisodesMemory | 情景记忆 | episodes_memories |
| SemanticMemory | 语义记忆 | semantic_memories |
| KnowledgeBase | 知识库 | knowledge_bases |
| UsageRecord | 用量记录 | usage_records |

#### User（用户）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | str | PK | 用户 ID（UUID 字符串） |
| username | str | UNIQUE, NOT NULL | 用户名 |
| email_encrypted | bytes | | 加密的邮箱 |
| password_hash | str | NOT NULL | 密码哈希 |
| role | str | DEFAULT 'user' | 角色 |
| preferences | JSON | | 用户偏好设置 |
| is_active | bool | DEFAULT True | 是否激活 |
| created_at | datetime | NOT NULL | 创建时间 |
| updated_at | datetime | | 更新时间 |

#### Session（会话）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| user_id | str | PK, FK | 用户 ID（复合主键之一） |
| session_seq | int | PK | 会话序号（复合主键之一，每用户从1递增） |
| id | str | UNIQUE, NOT NULL | 全局唯一 ID（格式：thread-{user_id_short}-{session_seq}） |
| agent_id | str | FK | 绑定的主 Agent ID |
| title | str | | 会话标题 |
| status | str | NOT NULL | 状态（active/archived/deleted） |
| created_at | datetime | NOT NULL | 创建时间 |
| updated_at | datetime | | 更新时间 |

> **设计说明**：Session 采用复合主键（user_id + session_seq），每个用户的会话序号独立从1开始递增。极简设计，只保留基础信息，其他数据由 execution_records 和 episodes_memory 承载。

#### ExecutionRecord（执行记录）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | str | PK | 编码 ID（嵌套ID格式，最大42字符） |
| session_id | str | FK, NOT NULL | 所属会话 ID |
| parent_record_id | str | FK | 父记录 ID（支持嵌套） |
| message_data | JSON | NOT NULL | 完整消息数据（包含所有执行细节） |
| created_at | datetime | NOT NULL | 创建时间 |

> **设计说明**：ExecutionRecord 采用极简设计，仅 5 个核心字段。所有执行细节（type、content、thinking、tool_calls、status、input、output、error 等）存储在 message_data JSON 字段中。通过 parent_record_id 支持任意深度嵌套。

#### AgentConfig（Agent 配置）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | str | PK | Agent ID（UUID 字符串） |
| config_id | str | UNIQUE, NOT NULL | 配置唯一标识 |
| name | str | NOT NULL | Agent 名称 |
| description | str | | Agent 描述 |
| agent_type | str | DEFAULT 'atomic' | Agent 类型（原子智能体） |
| model_name | str | NOT NULL | 模型名称 |
| model_params | JSON | | 模型参数 |
| system_prompt | str | NOT NULL | 系统提示词 |
| tool_ids | JSON | | 工具 ID 列表 |
| hard_constraints | JSON | | 硬约束列表 |
| soft_constraints | JSON | | 软约束列表 |
| static_vars | JSON | | 静态变量配置（第1层提示词） |
| dynamic_vars | JSON | | 动态变量配置（第4层提示词） |
| context_variables | JSON | | 上下文变量 |
| input_schema | JSON | | 输入 Schema |
| output_schema | JSON | | 输出 Schema |
| version | str | DEFAULT '1.0.0' | 版本号 |
| is_active | bool | DEFAULT True | 是否激活 |
| max_iterations | int | DEFAULT 10 | 最大迭代次数 |
| timeout_seconds | int | DEFAULT 300 | 超时时间（秒） |
| tags | JSON | | 标签列表 |
| agent_metadata | JSON | | 元数据 |
| status | str | DEFAULT 'active' | 状态 |
| level | int | DEFAULT 1 | Agent 层级（1=L1, 2=L2, 3=L3） |
| created_at | datetime | NOT NULL | 创建时间 |
| updated_at | datetime | | 更新时间 |

#### Task（任务）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | UUID | PK | 任务 ID |
| thread_id | str | FK | 会话 ID |
| title | str | NOT NULL | 标题 |
| description | str | | 描述 |
| status | str | NOT NULL | 状态 |
| priority | int | | 优先级 |
| phase | str | | 阶段 |
| created_at | datetime | | 创建时间 |
| updated_at | datetime | | 更新时间 |

### API 设计

#### 模块 API

| 方法 | 职责 |
|------|------|
| `get_db_manager() -> DatabaseManager` | 获取数据库管理器单例 |
| `get_async_session() -> AsyncGenerator[AsyncSession]` | 获取异步会话（FastAPI 依赖） |
| `get_session_context() -> AsyncContextManager` | 获取会话上下文（非 FastAPI 场景） |
| `DatabaseManager.get_session() -> AsyncGenerator` | 获取会话 |
| `DatabaseManager.close() -> None` | 关闭连接 |
| `DatabaseManager.create_all() -> None` | 创建所有表 |

### 配置设计

#### 模块配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| database_url | 数据库连接 URL | sqlite+aiosqlite:///./data.db |
| db_echo | 是否打印 SQL | False |
| db_pool_size | 连接池大小 | 5 |
| db_max_overflow | 最大溢出连接 | 10 |
| db_pool_timeout | 连接池超时（秒） | 30 |

### 错误处理

#### 模块错误码

| 错误码 | 说明 |
|--------|------|
| DB_CONN_001 | 数据库连接失败 |
| DB_CONN_002 | 连接池耗尽 |
| DB_EXEC_001 | 查询执行失败 |
| DB_EXEC_002 | 事务提交失败 |
| DB_NOTF_001 | 记录未找到 |

#### 异常类型

| 异常 | 说明 |
|------|------|
| DatabaseException | 数据库基础异常 |
| ConnectionError | 连接错误 |
| QueryError | 查询错误 |

### 安全设计

#### 模块安全

- 连接池：防止连接泄漏
- 事务管理：自动提交/回滚
- 参数化查询：防止 SQL 注入
- 敏感数据：密码哈希存储

## 结构

### 组件清单（文件夹 - 抽象说明）

| 组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| models/ | ORM 模型定义 | 输入：模型类 → 输出：数据库表 | - |
| repositories/ | 仓储模式数据访问 | 输入：查询条件 → 输出：模型实例 | - |
| migrations/ | 数据库迁移 | 输入：迁移脚本 → 输出：数据库变更 | - |
| triggers/ | 数据库触发器 | 输入：触发器定义 → 输出：触发器文件 | - |

### 文件清单（代码文件 - 具体接口）

#### connection.py
职责：数据库连接管理
暴露接口：
- `DatabaseManager`：数据库管理器类
  - `__init__(database_url: str | None)`
  - `get_session() -> AsyncGenerator[AsyncSession]`
  - `async close() -> None`
  - `async create_all() -> None`
  - `async drop_all() -> None`
- `get_db_manager() -> DatabaseManager`
- `get_async_session() -> AsyncGenerator[AsyncSession]`
- `get_session_context() -> AsyncContextManager`

#### models/base.py
职责：模型基类
暴露接口：
- `Base`：SQLAlchemy 声明基类

#### models/user.py
职责：用户模型
暴露接口：
- `User`：用户模型类
- `Session`：会话模型类

#### models/execution.py
职责：执行记录模型
暴露接口：
- `ExecutionRecord`：执行记录模型类

#### models/agent.py
职责：Agent 配置模型
暴露接口：
- `AgentConfig`：Agent 配置模型类

#### models/task.py
职责：任务模型
暴露接口：
- `Task`：任务模型类
- `EvaluationMetric`：评估指标模型类

#### models/workflow.py
职责：工作流模型
暴露接口：
- `Workflow`：工作流模型类
- `WorkflowComposition`：工作流组合模型类

#### models/tool.py
职责：工具模型
暴露接口：
- `ToolLibrary`：工具库模型类

#### models/memory.py
职责：记忆模型
暴露接口：
- `EpisodesMemory`：情景记忆模型类
- `SemanticMemory`：语义记忆模型类
- `KnowledgeBase`：知识库模型类
- `Tag`：标签模型类
- `MemoryTag`：记忆标签关联模型类
- `MemoryChunk`：记忆块模型类
- `TagCooccurrence`：标签共现模型类

#### models/monitoring.py
职责：监控模型
暴露接口：
- `UsageRecord`：用量记录模型类
- `UsageStatistics`：用量统计模型类
- `MonitoringAlert`：监控告警模型类
- `TaskQueueStats`：任务队列统计模型类

#### repositories/base.py
职责：仓储基类
暴露接口：
- `BaseRepository`：仓储基类
  - `async get_by_id(id: Any) -> Model | None`
  - `async get_all() -> list[Model]`
  - `async create(entity: Model) -> Model`
  - `async update(entity: Model) -> Model`
  - `async delete(id: Any) -> bool`

#### repositories/user_repository.py
职责：用户仓储
暴露接口：
- `UserRepository`：用户仓储类
  - `async get_by_username(username: str) -> User | None`
  - `async update_last_login(user_id: UUID) -> None`

#### repositories/execution_record_repo.py
职责：执行记录仓储
暴露接口：
- `ExecutionRecordRepository`：执行记录仓储类

#### migrations/manager.py
职责：迁移管理
暴露接口：
- `MigrationManager`：迁移管理器类

#### migrations/runner.py
职责：迁移执行
暴露接口：
- `MigrationRunner`：迁移执行器类

### 测试策略

#### 模块测试

- 单元测试：模型定义、仓储方法
- 集成测试：数据库操作、事务管理
- 测试覆盖：核心逻辑 ≥85%

## 实现

→ 见各组件代码文件
