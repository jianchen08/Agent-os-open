# 任务系统 (tasks)

## 需求

Agent OS 需要统一的任务生命周期管理：
- 任务从创建到完成经历 6 种状态：pending → running → evaluating → completed/failed，支持 paused 暂停
- 状态转换必须合法，非法转换抛出 InvalidTransitionError
- 任务需持久化存储（JSON 文件），支持按状态/父任务查询
- 父任务进度由子任务完成比例决定
- TaskService 作为业务编排层，组合状态机、存储和进度计算器

## 逻辑

### 类型定义 (types.py)
- `TaskStatus`：6 状态枚举（PENDING / RUNNING / EVALUATING / COMPLETED / FAILED / PAUSED）
- `TaskModel`：任务核心数据结构，包含 id、title、status、priority、agent_level、parent_task_id 等
- `AC`：验收标准数据类（metric_id + pass_threshold）
- `create_task()`：工厂函数，便捷创建 TaskModel

### 状态机 (state_machine.py)
- 6 状态有限状态机，定义合法转换映射
- `can_transition()` 检查合法性，`transition()` 执行转换
- 终态（COMPLETED / FAILED）不可再转换
- 非法转换抛出 `InvalidTransitionError(from_status, to_status)`

### 存储层 (storage.py)
- 内存 dict 缓存 + JSON 文件持久化
- 支持 CRUD：save / get / update / delete
- 查询：list_by_status / list_by_parent
- JSON 文件损坏时优雅降级（从空开始）

### 进度计算 (progress.py)
- 等权平均：completed 算 100%，其他算 0%
- `calculate(subtask_statuses)` 和 `calculate_from_tasks(tasks)` 两种入口
- 无子任务返回 0.0

### 服务层 (service.py + Mixin 拆分)
- 依赖注入组合：StateMachine + TaskStorage + ProgressCalculator
- 可选注入：scheduler / concurrency（来自 infrastructure 层）
- 提供完整生命周期操作：create / start / pause / resume / fail / move_to_evaluating / complete_evaluation
- 查询：get_task / list_by_status / list_subtasks / get_progress
- **拆分架构**：service.py 为门面类（164行），通过多重继承组合 3 个 Mixin：
  - `_task_crud.py`：创建、查询、字段更新与基础删除
  - `_task_state.py`：状态转换、幽灵清理与评估完成
  - `_task_cleanup.py`：工作空间清理、级联删除与容器管理

## 结构

### 文件清单

| 文件 | 行数 | 用途 |
|------|------|------|
| `tasks/__init__.py` | 10 | 导出所有公共类型与服务 |
| `tasks/types.py` | 121 | TaskStatus, AC, TaskModel, create_task |
| `tasks/state_machine.py` | 100 | StateMachine + InvalidTransitionError |
| `tasks/storage.py` | 174 | TaskStorage JSON 持久化 |
| `tasks/service.py` | 164 | TaskService 门面类（组合 Mixin） |
| `tasks/_task_crud.py` | 336 | CRUD Mixin：创建/查询/更新/删除 |
| `tasks/_task_state.py` | 625 | 状态 Mixin：状态转换/幽灵清理/评估 |
| `tasks/_task_cleanup.py` | 609 | 清理 Mixin：工作空间/级联/容器删除 |
| `tasks/timer_manager.py` | 500+ | 任务超时定时器管理 |
| `tasks/workspace.py` | 70 | 任务工作空间辅助 |
| `tasks/services/` | 目录 | 数据库版本的服务层（独立体系） |

### 依赖关系

```
TaskService → _TaskCrudMixin (数据操作)
            → _TaskStateMixin (状态转换)
            → _TaskCleanupMixin (资源清理)
            → StateMachine (状态机校验)
            → TaskStorage (持久化)
            → pipeline.types (AgentLevel, TaskPriority, 复用枚举)

TaskModel → pipeline.types (AgentLevel, TaskPriority)
```

### 状态转换图

```
pending ──→ running ──→ evaluating ──→ completed
               │              │
               ├──→ failed    └──→ failed
               │
               └──→ paused ──→ running（恢复）
```
