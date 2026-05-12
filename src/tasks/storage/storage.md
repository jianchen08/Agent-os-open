# 任务存储组件

## 一、需求

### 1.1 组件职责

任务存储组件提供任务数据的持久化能力，负责：
- 定义存储抽象接口（ITaskStorage）
- 提供数据库存储实现（DatabaseTaskStorage）
- 提供文件存储实现（FileTaskStorage）
- 存储工厂模式管理

### 1.2 对外接口

- `ITaskStorage`：存储抽象接口
- `DatabaseTaskStorage`：数据库存储实现
- `FileTaskStorage`：文件存储实现
- `TaskStorageFactory`：存储工厂

### 1.3 依赖

- `tasks.models`：任务数据模型
- `core.database`：数据库连接
- `core.config`：配置模块

---

## 二、逻辑

### 2.1 流程设计

#### 存储操作流程

```
Service层 → TaskStorageFactory
              ↓
         创建存储实例
              ↓
         ITaskStorage接口调用
              ↓
    DatabaseTaskStorage / FileTaskStorage
              ↓
         数据持久化
```

#### 工厂创建流程

```
配置类型 → TaskStorageFactory.create()
              ↓
    "database" → DatabaseTaskStorage
    "file" → FileTaskStorage
```

### 2.2 数据流向

```
Service层 → ITaskStorage接口
                ↓
    ┌───────────┴───────────┐
    ↓                       ↓
DatabaseTaskStorage    FileTaskStorage
    ↓                       ↓
  数据库                   文件系统
```

### 2.3 数据模型

#### TaskModel

| 字段 | 类型 | 说明 |
|------|------|------|
| id | str | 任务唯一标识 |
| project_id | str | 所属项目ID |
| status | ExecutionStatus | 任务状态 |
| progress | float | 进度百分比 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

### 2.4 错误处理

- 存储操作失败：抛出 `StorageError`
- 任务不存在：抛出 `TaskNotFoundError`
- 数据库连接失败：抛出 `DatabaseConnectionError`

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| ITaskStorage | 存储抽象接口定义 |
| DatabaseTaskStorage | 数据库存储实现 |
| FileTaskStorage | 文件存储实现 |
| TaskStorageFactory | 存储实例工厂 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `base.py` | 抽象接口与基础模型 |
| `db_storage.py` | 数据库存储实现 |
| `file_storage.py` | 文件存储实现 |
| `factory.py` | 存储工厂 |

### 3.3 测试策略

- 单元测试：各存储方法的独立测试
- 集成测试：与真实数据库/文件系统的协作测试
- 覆盖率要求：核心逻辑 ≥90%

---

## 四、实现

### 4.1 base.py

```
ITaskStorage:
  create(task: TaskModel) -> TaskModel: 创建任务
  get(task_id: str) -> TaskModel: 获取任务
  update(task: TaskModel) -> TaskModel: 更新任务
  delete(task_id: str) -> bool: 删除任务
  list(filter: TaskFilter) -> List[TaskModel]: 查询任务列表

TaskModel:
  id: str
  project_id: str
  status: ExecutionStatus
  progress: float
  created_at: datetime
  updated_at: datetime

StorageError(Exception): 存储操作异常基类
```

### 4.2 db_storage.py

```
DatabaseTaskStorage(ITaskStorage):
  create(task: TaskModel) -> TaskModel: 数据库创建任务
  get(task_id: str) -> TaskModel: 数据库查询任务
  update(task: TaskModel) -> TaskModel: 数据库更新任务
  delete(task_id: str) -> bool: 数据库删除任务
  list(filter: TaskFilter) -> List[TaskModel]: 数据库查询列表
```

### 4.3 file_storage.py

```
FileTaskStorage(ITaskStorage):
  create(task: TaskModel) -> TaskModel: 文件创建任务
  get(task_id: str) -> TaskModel: 文件读取任务
  update(task: TaskModel) -> TaskModel: 文件更新任务
  delete(task_id: str) -> bool: 文件删除任务
  list(filter: TaskFilter) -> List[TaskModel]: 文件扫描列表
```

### 4.4 factory.py

```
TaskStorageFactory:
  create(storage_type: str) -> ITaskStorage: 创建存储实例
  get_default() -> ITaskStorage: 获取默认存储实例
```
