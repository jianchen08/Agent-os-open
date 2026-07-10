# 回滚机制模块

## 一、需求

### 1.1 模块职责

提供通用的操作回滚能力，支持：
- 操作日志记录：记录文件、Git、API 等操作的执行详情
- 检查点管理：在关键节点创建检查点，支持回滚到指定检查点
- 回滚执行：自动执行逆操作，恢复系统状态

### 1.2 对外接口

```python
# 管理器
class RollbackManager:
    async def create_checkpoint(task_id, name, description, metadata) -> str
    async def record_operation(task_id, tool_name, operation_type, target, params, ...) -> str
    async def rollback(task_id, to_checkpoint, steps) -> RollbackResult

# 集成
class TaskRollbackIntegration:
    async def on_message_start(session_id, message_id) -> str
    async def on_regenerate(session_id, original_message_id) -> RollbackResult

# 装饰器
@reversible_operation(tool_name, operation_type, target_param)
```

### 1.3 依赖

- `sqlalchemy`：数据库操作
- `aiohttp`：HTTP 请求（API 逆操作）
- `pydantic`：数据验证

---

## 二、逻辑

### 2.1 核心流程

```
┌─────────────────────────────────────────────────────────────┐
│                      操作执行流程                            │
├─────────────────────────────────────────────────────────────┤
│  1. 创建检查点（可选）                                        │
│  2. 执行操作前捕获状态                                        │
│  3. 执行操作                                                  │
│  4. 执行后捕获状态                                            │
│  5. 记录操作日志                                              │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      回滚执行流程                            │
├─────────────────────────────────────────────────────────────┤
│  1. 获取需要回滚的操作列表                                    │
│  2. 按序号倒序遍历                                            │
│  3. 查找对应的逆操作器                                        │
│  4. 执行逆操作                                                │
│  5. 更新操作状态                                              │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 数据模型

#### OperationType（操作类型）
| 值 | 说明 |
|---|---|
| CREATE | 创建操作 |
| UPDATE | 更新操作 |
| DELETE | 删除操作 |
| EXECUTE | 执行操作 |

#### OperationStatus（操作状态）
| 值 | 说明 |
|---|---|
| EXECUTED | 已执行 |
| ROLLED_BACK | 已回滚 |
| FAILED | 失败 |

#### OperationLog（操作日志）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 操作日志 ID |
| task_id | str | 任务 ID |
| checkpoint_id | str | None | 检查点 ID |
| tool_name | str | 工具名称 |
| operation_type | OperationType | 操作类型 |
| target | str | 操作目标 |
| params | dict | 操作参数 |
| before_state | dict | None | 操作前状态 |
| after_state | dict | None | 操作后状态 |
| reversible | bool | 是否可逆 |
| reverse_action | dict | None | 逆操作定义 |
| sequence | int | 操作序号 |
| status | OperationStatus | 操作状态 |
| error_message | str | None | 错误信息 |
| created_at | datetime | 创建时间 |

#### Checkpoint（检查点）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 检查点 ID |
| task_id | str | 任务 ID |
| name | str | None | 检查点名称 |
| description | str | None | 检查点描述 |
| metadata | dict | 元数据 |
| created_at | datetime | 创建时间 |

#### RollbackResult（回滚结果）
| 字段 | 类型 | 说明 |
|---|---|---|
| success | bool | 是否成功 |
| rolled_back_count | int | 已回滚数量 |
| skipped_count | int | 跳过数量 |
| failed_count | int | 失败数量 |
| operations | list | 操作列表 |
| warnings | list | 警告列表 |
| errors | list | 错误列表 |

### 2.3 逆操作器设计

逆操作器负责执行各类操作的逆操作：

| 逆操作器 | 支持的工具 | 说明 |
|---|---|---|
| FileReverser | file_read, file_write, file_create, file_delete, file_update | 文件操作逆操作 |
| GitReverser | git_commit, git_branch, git_stash, git_checkout | Git 操作逆操作 |
| APIReverser | api_create, api_update, api_delete, http_request | API 操作逆操作 |

### 2.4 错误处理

- 操作不可逆：跳过并记录警告
- 逆操作器不存在：跳过并记录警告
- 逆操作执行失败：记录错误，继续执行后续操作

---

## 三、结构

### 3.1 组件清单

| 组件 | 职责 |
|---|---|
| RollbackManager | 回滚管理器，提供检查点和操作日志管理 |
| TaskRollbackIntegration | 任务集成，与任务执行流程集成 |
| ReverserRegistry | 逆操作器注册表 |
| BaseReverser | 逆操作器基类 |
| FileReverser | 文件操作逆操作器 |
| GitReverser | Git 操作逆操作器 |
| APIReverser | API 操作逆操作器 |
| OperationRecorder | 操作记录器 |

### 3.2 文件清单

| 文件 | 说明 |
|---|---|
| `__init__.py` | 模块导出 |
| `models.py` | 数据模型定义 |
| `manager.py` | 回滚管理器 |
| `reversers.py` | 逆操作器实现 |
| `decorators.py` | 装饰器 |
| `integration.py` | 任务集成 |

### 3.3 测试策略

- 单元测试：测试各逆操作器的逆操作逻辑
- 集成测试：测试完整的回滚流程
- 边界测试：测试不可逆操作、空操作列表等边界情况
