# 看门狗组件

## 一、需求

### 1.1 组件职责

看门狗组件负责任务的自动监控与执行控制，核心职责：
- 监控任务执行状态
- 检测超时与卡死任务
- 触发任务执行
- 处理执行失败
- 项目级控制（暂停/恢复/完成）

### 1.2 对外接口

- `AutoExecuteWatchdog`：看门狗主协调器
- 通过内部组件提供具体能力

### 1.3 依赖

- `tasks.storage`：任务存储组件
- `tasks.services`：任务服务组件
- `core.logging`：日志模块
- `core.config`：配置模块

---

## 二、逻辑

### 2.1 流程设计

#### 主监控循环

```
定时触发 → AutoExecuteWatchdog.run()
              ↓
         TaskMonitor.check()
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  超时检测  卡死检测  项目检查
    ↓         ↓         ↓
TimeoutHandler  FailureHandler  ProjectController
    ↓         ↓         ↓
         TaskTrigger.execute()
              ↓
         状态更新与通知
```

#### 超时处理流程

```
任务超时 → TimeoutHandler.handle()
              ↓
         判断超时类型
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  软超时    硬超时    卡死
    ↓         ↓         ↓
  发送通知  强制终止  标记失败
```

#### 失败处理流程

```
任务失败 → FailureHandler.handle()
              ↓
         异常分类
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  可重试    不可重试  需人工
    ↓         ↓         ↓
  自动重试  标记失败  创建审批
```

### 2.2 数据流向

```
定时器 → Watchdog → Monitor → Storage
                        ↓
              ┌─────────┼─────────┐
              ↓         ↓         ↓
          TimeoutHandler FailureHandler ProjectController
              ↓         ↓         ↓
              └─────────┼─────────┘
                        ↓
                   Trigger → Service → Storage
```

### 2.3 配置设计

| 配置项 | 类型 | 说明 |
|--------|------|------|
| check_interval | int | 检查间隔（秒） |
| soft_timeout | int | 软超时时间（秒） |
| hard_timeout | int | 硬超时时间（秒） |
| max_retry_count | int | 最大重试次数 |
| stuck_threshold | int | 卡死判定阈值 |

### 2.4 错误处理

- 任务执行失败：由 FailureHandler 分类处理
- 超时任务：由 TimeoutHandler 分级处理
- 存储操作失败：记录日志并重试

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| TaskMonitor | 任务监控与项目检查 |
| TaskTrigger | 任务执行触发 |
| TimeoutHandler | 超时与卡死处理 |
| FailureHandler | 失败异常处理 |
| ProjectController | 项目级控制 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `watchdog.py` | 看门狗主协调器 |
| `components/monitor.py` | 任务监控组件 |
| `components/trigger.py` | 任务触发组件 |
| `components/timeout_handler.py` | 超时处理组件 |
| `components/failure_handler.py` | 失败处理组件 |
| `components/project_controller.py` | 项目控制组件 |

### 3.3 测试策略

- 单元测试：各组件方法的独立测试
- 集成测试：看门狗完整流程测试
- 覆盖率要求：核心逻辑 ≥90%

---

## 四、实现

### 4.1 watchdog.py

```
AutoExecuteWatchdog:
  run() -> None: 执行一次监控检查
  start() -> None: 启动定时监控
  stop() -> None: 停止监控
  register_callback(event: str, callback: Callable) -> None: 注册事件回调
```
