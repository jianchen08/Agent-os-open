# 看门狗子组件

## 一、需求

### 1.1 组件职责

看门狗子组件提供具体的监控与控制能力：
- TaskMonitor：任务状态监控、项目检查
- TaskTrigger：任务执行触发
- TimeoutHandler：超时与卡死检测处理
- FailureHandler：差异化异常处理
- ProjectController：项目级暂停/恢复/完成控制

### 1.2 对外接口

通过 `AutoExecuteWatchdog` 统一协调，各子组件提供具体能力。

### 1.3 依赖

- `tasks.storage`：任务存储组件
- `tasks.services`：任务服务组件
- `core.logging`：日志模块

---

## 二、逻辑

### 2.1 流程设计

#### TaskMonitor 流程

```
check() → 扫描运行中任务
              ↓
         检查每个任务状态
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  正常执行  需要处理  项目暂停
    ↓         ↓         ↓
  继续监控  返回待处理  跳过
```

#### TaskTrigger 流程

```
execute(task_id) → 验证任务状态
                      ↓
                 准备执行环境
                      ↓
                 调用执行服务
                      ↓
                 更新任务状态
```

#### TimeoutHandler 流程

```
handle(task) → 计算运行时间
                  ↓
             判断超时类型
                  ↓
    ┌─────────────┼─────────────┐
    ↓             ↓             ↓
  < soft_timeout  soft~hard    > hard_timeout
    ↓             ↓             ↓
  正常         发送警告       强制终止
```

#### FailureHandler 流程

```
handle(task, error) → 异常分类
                        ↓
    ┌───────────────────┼───────────────────┐
    ↓                   ↓                   ↓
  TransientError    PermanentError     HumanRequiredError
    ↓                   ↓                   ↓
  自动重试           标记失败           创建审批请求
```

#### ProjectController 流程

```
control() → 扫描项目状态
               ↓
    ┌──────────┼──────────┐
    ↓          ↓          ↓
  活跃项目   暂停项目   完成项目
    ↓          ↓          ↓
  正常监控   跳过任务   归档处理
```

### 2.2 数据流向

```
Watchdog
    ↓
Monitor → Storage（读取任务）
    ↓
┌───┼───┐
↓   ↓   ↓
Timeout Failure Project
Handler Handler Controller
↓   ↓   ↓
└───┼───┘
    ↓
Trigger → Service（执行操作）
    ↓
Storage（更新状态）
```

### 2.3 错误处理

| 组件 | 错误类型 | 处理方式 |
|------|----------|----------|
| TaskMonitor | 存储读取失败 | 记录日志，下次重试 |
| TaskTrigger | 执行失败 | 交给 FailureHandler |
| TimeoutHandler | 强制终止失败 | 标记为卡死 |
| FailureHandler | 重试次数耗尽 | 标记为失败 |
| ProjectController | 项目状态异常 | 记录日志，人工介入 |

---

## 三、结构

### 3.1 子组件清单

无更深层子组件。

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `monitor.py` | 任务监控组件 |
| `trigger.py` | 任务触发组件 |
| `timeout_handler.py` | 超时处理组件 |
| `failure_handler.py` | 失败处理组件 |
| `project_controller.py` | 项目控制组件 |

### 3.3 测试策略

- 单元测试：各组件方法独立测试
- 集成测试：组件协作流程测试
- 覆盖率要求：核心逻辑 ≥90%

---

## 四、实现

### 4.1 monitor.py

```
TaskMonitor:
  check() -> List[TaskCheckResult]: 执行监控检查
  check_task(task_id: str) -> TaskCheckResult: 检查单个任务
  check_project(project_id: str) -> ProjectStatus: 检查项目状态
```

### 4.2 trigger.py

```
TaskTrigger:
  execute(task_id: str) -> ExecutionResult: 触发任务执行
  prepare_environment(task: Task) -> ExecutionContext: 准备执行环境
```

### 4.3 timeout_handler.py

```
TimeoutHandler:
  handle(task: Task) -> TimeoutAction: 处理超时任务
  check_timeout(task: Task) -> TimeoutType: 检查超时类型
  force_terminate(task_id: str) -> bool: 强制终止任务
```

### 4.4 failure_handler.py

```
FailureHandler:
  handle(task: Task, error: Exception) -> FailureAction: 处理失败任务
  classify_error(error: Exception) -> ErrorType: 分类异常类型
  should_retry(task: Task, error: Exception) -> bool: 判断是否重试
```

### 4.5 project_controller.py

```
ProjectController:
  pause_project(project_id: str) -> bool: 暂停项目
  resume_project(project_id: str) -> bool: 恢复项目
  complete_project(project_id: str) -> bool: 完成项目
  get_project_status(project_id: str) -> ProjectStatus: 获取项目状态
```
