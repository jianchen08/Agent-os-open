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

### 服务层 (service.py)
- 依赖注入组合：StateMachine + TaskStorage + ProgressCalculator
- 可选注入：scheduler / concurrency（来自 infrastructure 层）
- 提供完整生命周期操作：create / start / pause / resume / fail / move_to_evaluating / complete_evaluation
- 查询：get_task / list_by_status / list_subtasks / get_progress

## 结构

### 文件清单

| 文件 | 行数 | 用途 |
|------|------|------|
| `tasks/__init__.py` | 20 | 导出所有公共类型与服务 |
| `tasks/types.py` | 121 | TaskStatus, AC, TaskModel, create_task |
| `tasks/state_machine.py` | 100 | StateMachine + InvalidTransitionError |
| `tasks/storage.py` | 174 | TaskStorage JSON 持久化 |
| `tasks/progress.py` | 50 | ProgressCalculator |
| `tasks/service.py` | 190 | TaskService 业务编排 |
| `tests/test_tasks.py` | ~250 | M5a 单元测试 |

### 依赖关系

```
TaskService → StateMachine (状态转换)
            → TaskStorage (持久化)
            → ProgressCalculator (进度)
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
