# 服务模块

## 一、需求

### 1.1 模块职责

提供业务服务层，封装业务逻辑：
- 任务服务：任务的 CRUD 操作和执行管理
- 工具服务：工具注册、执行、权限管理
- 工作流服务：工作流的 CRUD 和执行
- 同步服务：配置文件到数据库的同步
- 看门狗服务：任务监控和自动执行

### 1.2 对外接口

```python
# 基础服务
class BaseService:
    async def _get_session() -> AsyncSession
    async def _commit_transaction()
    async def _rollback_transaction()

# 任务服务
class TaskCommandService:
    async def create_task(task_data, user_id, session_id) -> tuple[Task, dict]
    async def update_task(task_id, task_data, user_id, session_id) -> dict
    async def delete_task(task_id, user_id, session_id) -> bool

class TaskQueryService:
    async def list_tasks(user_id, skip, limit, ...) -> list[dict]
    async def get_task(task_id, user_id, ...) -> dict
    async def get_evaluation_status(task_id, user_id) -> dict

class TaskExecutionService:
    async def execute_task(task_id, session, ...) -> dict
    async def check_timeout_tasks() -> dict

class TaskService:  # 综合服务
    async def create_task(task_data, user_id, session_id) -> dict
    async def list_tasks(...) -> list[dict]
    async def get_task(...) -> dict
    async def update_task(...) -> dict
    async def delete_task(...) -> bool

# 工具服务
class ToolService:
    async def register_tool(tool) -> bool
    async def get_tool_suggestions(context, user_id) -> list[Tool]
    async def get_available_tools(user_id, user_roles) -> list[Tool]
    async def list_tools(page, page_size, ...) -> dict
    async def get_tool(tool_name) -> dict
    async def generate_tool(name, description, ...) -> dict

class ToolPermissionManager:
    def add_user_permission(user_id, permission)
    def check_permission(user_id, tool_name, user_roles) -> bool

class ToolUsageTracker:
    def record_call(tool_name, success, duration, ...)
    def get_stats(tool_name) -> dict

class ToolSyncService:
    async def sync_tool_to_db(tool) -> str
    async def sync_all_builtin_tools() -> SyncResult

# 工作流服务
class WorkflowService:
    async def list_workflows(user_id, page, ...) -> dict
    async def get_workflow(workflow_id, user_id) -> dict
    async def create_workflow(user_id, name, definition, ...) -> dict
    async def update_workflow(workflow_id, user_id, **kwargs) -> dict
    async def delete_workflow(workflow_id, user_id)
    async def execute_workflow(workflow_id, inputs, ...) -> dict

class WorkflowSyncService:
    async def sync_all(session, force) -> dict
    async def validate_workflows(session) -> dict

# 看门狗服务
class WatchdogServiceManager:
    async def start()
    async def stop()
    async def process_pending_tasks() -> dict
```

### 1.3 依赖

- `sqlalchemy`：数据库操作
- `pydantic`：数据验证
- `src.db`：数据库模型和仓储
- `src.core`：核心组件（事件总线、DI 容器）
- `src.tasks`：任务执行器
- `src.tools`：工具执行器

---

## 二、逻辑

### 2.1 服务分层

```
┌─────────────────────────────────────────────────────────────┐
│                      API 层                                  │
├─────────────────────────────────────────────────────────────┤
│                      服务层                                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │
│  │ TaskService  │ │ ToolService  │ │WorkflowService│       │
│  └──────────────┘ └──────────────┘ └──────────────┘        │
├─────────────────────────────────────────────────────────────┤
│                      仓储层                                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │
│  │ TaskRepo     │ │ ToolRepo     │ │ WorkflowRepo │        │
│  └──────────────┘ └──────────────┘ └──────────────┘        │
├─────────────────────────────────────────────────────────────┤
│                      数据库层                                │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 任务服务设计

#### CQRS 模式
任务服务采用 CQRS（命令查询职责分离）模式：
- `TaskCommandService`：负责创建、更新、删除操作
- `TaskQueryService`：负责查询操作
- `TaskExecutionService`：负责执行操作
- `TaskService`：综合服务，提供统一接口

#### 任务执行流程
```
1. 创建任务记录
2. 验证评估指标
3. 创建执行记录
4. 发布任务提交事件
5. 后台执行任务
```

### 2.3 工具服务设计

#### 权限管理
```
用户权限 -> 角色权限 -> 工具所需权限
         ↓
    检查用户是否有权限使用工具
```

#### 使用统计
```
记录调用 -> 更新统计 -> 生成报告
         ↓
    工具调用次数、成功率、平均耗时
```

### 2.4 同步服务设计

#### YAML 配置同步
```
扫描 YAML 文件 -> 计算 checksum -> 对比数据库
                                      ↓
                        新增/更新/跳过
```

### 2.5 错误处理

- 权限验证失败：返回 None 或 False
- 资源不存在：抛出 NotFoundException
- 参数验证失败：抛出 ValidationException
- 执行失败：返回包含 error 字段的结果

---

## 三、结构

### 3.1 组件清单

| 组件 | 职责 |
|---|---|
| BaseService | 基础服务类，提供会话管理 |
| TaskCommandService | 任务命令服务 |
| TaskQueryService | 任务查询服务 |
| TaskExecutionService | 任务执行服务 |
| TaskService | 任务综合服务 |
| ToolService | 工具业务服务 |
| ToolPermissionManager | 工具权限管理器 |
| ToolUsageTracker | 工具使用统计跟踪器 |
| ToolSyncService | 工具同步服务 |
| ToolMarketplaceService | 工具市场服务 |
| WorkflowService | 工作流服务 |
| WorkflowSyncService | 工作流同步服务 |
| WatchdogServiceManager | 看门狗服务管理器 |
| YamlConfigSyncService | YAML 配置同步基类 |

### 3.2 文件清单

| 文件 | 说明 |
|---|---|
| `base.py` | 基础服务类 |
| `task_command_service.py` | 任务命令服务 |
| `task_query_service.py` | 任务查询服务 |
| `task_execution_service.py` | 任务执行服务 |
| `task_service.py` | 任务综合服务 |
| `tool_service.py` | 工具业务服务 |
| `tool_permission_service.py` | 工具权限管理服务 |
| `tool_usage_service.py` | 工具使用统计服务 |
| `tool_sync_service.py` | 工具同步服务 |
| `tool_marketplace_service.py` | 工具市场服务 |
| `workflow_service.py` | 工作流服务 |
| `workflow_sync_service.py` | 工作流同步服务 |
| `watchdog_service.py` | 看门狗服务 |
| `sync/base.py` | 同步服务基类 |

### 3.3 测试策略

- 单元测试：测试各服务的业务逻辑
- 集成测试：测试服务与数据库的交互
- Mock 测试：测试服务间的依赖关系
