# 仓储组件

## 需求
### 职责
提供数据访问层的仓储模式实现，封装数据库操作，提供类型安全的数据访问接口。

### 对外接口
- 输入：查询条件、实体数据
- 输出：实体对象、查询结果列表

### 依赖
- 外部依赖：sqlalchemy
- 内部依赖：src.db.models（数据模型）

## 逻辑
### 流程设计
1. **创建**：接收数据字典，创建实体并持久化
2. **查询**：根据条件查询，返回实体或列表
3. **更新**：根据 ID 更新实体字段
4. **删除**：根据 ID 删除实体

### 数据流向
```
业务层 -> Repository -> SQLAlchemy Session -> 数据库
```

### 数据模型
#### BaseRepository（基础仓储）
| 方法 | 说明 |
|---|---|
| create(**kwargs) -> T | 创建实体 |
| get(id) -> T | 根据 ID 获取 |
| get_by(**kwargs) -> T | 根据条件获取单个 |
| get_all(limit, offset, order_by) -> list[T] | 获取所有 |
| find_by(**kwargs) -> list[T] | 根据条件查找 |
| update(id, data) -> bool | 更新实体 |
| delete(id) -> bool | 删除实体 |
| count(**kwargs) -> int | 统计数量 |

## 结构
### 子组件清单
无

### 文件清单（代码文件 - 具体接口）
#### base.py
职责：基础仓储类
暴露接口：
- `BaseRepository[T]`：泛型基础仓储类
  - `__init__(session: AsyncSession, model_class: type[T])`：初始化
  - `create(**kwargs) -> T`：创建实体
  - `get(id: EntityId) -> T | None`：根据 ID 获取
  - `get_by(**kwargs) -> T | None`：根据条件获取单个
  - `get_all(limit: int, offset: int, order_by: str | None) -> list[T]`：获取所有
  - `find_by(limit: int, offset: int, order_by: str | None, **kwargs) -> list[T]`：根据条件查找
  - `update(id: EntityId, data: dict) -> bool`：更新实体
  - `delete(id: EntityId) -> bool`：删除实体
  - `count(**kwargs) -> int`：统计数量

#### task_repo.py
职责：任务仓储
暴露接口：
- `TaskRepository`：任务仓储类
  - 继承 BaseRepository[Task]
  - `create_task(task_data: dict) -> Task`：创建任务（幂等）
  - `get_task_with_metrics(task_id: str) -> dict | None`：获取任务及指标
  - `get_root_tasks(user_id: str, session_id: str, status: str, limit: int) -> list[Task]`：获取根任务
  - `get_tasks_by_status(status: str, session_id: str, limit: int) -> list[Task]`：按状态查询
  - `get_tasks_by_user(user_id: str, status: str, limit: int) -> list[Task]`：获取用户任务
  - `get_subtasks(parent_task_id: str) -> list[Task]`：获取子任务
  - `update_task_status(task_id: str, status: str, error_message: str | None) -> bool`：更新状态
  - `increment_retry_count(task_id: str) -> int`：增加重试计数
  - `get_tasks_by_execution_record(execution_record_id: str) -> list[Task]`：按执行记录查询
  - `count_by_status(session_id: str, user_id: str) -> dict[str, int]`：按状态统计
  - `get_pending_tasks(limit: int, priority_min: int) -> list[Task]`：获取待处理
  - `get_overdue_tasks(current_time: datetime) -> list[Task]`：获取过期任务

#### user_repository.py
职责：用户仓储
暴露接口：
- `UserRepository`：用户仓储类
  - 继承 BaseRepository
  - `get_by_id(user_id: UUID) -> UserInDB | None`：根据 ID 获取
  - `get_by_username(username: str) -> UserInDB | None`：根据用户名获取
  - `create(user_create: UserCreate, password_hash: str) -> UserInDB`：创建用户
  - `update_last_login(user_id: UUID) -> None`：更新最后登录时间
  - `update_role(user_id: UUID, new_role: str) -> UserInDB | None`：更新角色

#### execution_record_repo.py
职责：执行记录仓储
暴露接口：
- `ExecutionRecordRepository`：执行记录仓储类

#### evaluation_metric_repo.py
职责：评估指标仓储
暴露接口：
- `EvaluationMetricRepository`：评估指标仓储类

#### notification_repository.py
职责：通知仓储
暴露接口：
- `NotificationRepository`：通知仓储类

#### agent_call_repository.py
职责：Agent 调用记录仓储
暴露接口：
- `AgentCallRepository`：Agent 调用记录仓储类

#### __init__.py
职责：仓储模块导出
暴露接口：
- 导出所有仓储类

### 测试策略
#### 组件测试
- 单元测试：基础 CRUD 操作
- 集成测试：复杂查询、事务处理
- 覆盖率要求：核心逻辑 >= 85%

## 实现
-> 见代码文件：src/db/repositories/
