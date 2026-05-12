# 触发器模块

## 一、需求

### 1.1 模块职责

提供基于时间、事件和条件的触发器功能：
- 时间触发器：基于 Cron 表达式、间隔或单次触发
- 事件触发器：监听系统事件并执行动作
- 条件触发器：基于条件表达式判断是否触发

### 1.2 对外接口

```python
# 触发器管理器
class TriggerManager:
    async def start()
    async def stop()
    async def register_trigger(trigger_config) -> bool
    async def unregister_trigger(trigger_id) -> bool
    async def enable_trigger(trigger_id) -> bool
    async def disable_trigger(trigger_id) -> bool
    async def handle_event(event) -> list[ExecutionResult]
    def list_triggers() -> list[dict]
    def get_statistics() -> dict

# 触发器注册表
class TriggerRegistry:
    async def load_from_config()
    async def register_trigger(config) -> None
    async def unregister_trigger(trigger_id) -> None
    async def get_trigger(trigger_id) -> BaseTrigger
    async def list_triggers(enabled_only, trigger_type) -> list[BaseTrigger]
    def get_stats() -> dict

# 触发器状态管理器
class TriggerStateManager:
    async def update_trigger_state(trigger_id, state, ...)
    async def get_trigger_state(trigger_id) -> dict
    async def record_execution(trigger_id, success, execution_time, ...)
    async def get_execution_statistics(trigger_id, time_range) -> dict

# 触发器基类
class BaseTrigger:
    async def execute(*args, **kwargs) -> ExecutionResult
    async def execute_actions(context) -> ExecutionResult
    def validate() -> bool
    def to_dict() -> dict

# 具体触发器
class TimeTrigger(BaseTrigger): ...
class EventTrigger(BaseTrigger): ...
class ConditionTrigger(BaseTrigger): ...

# 动作执行器
class ActionExecutor:
    async def execute(action_config, context) -> ExecutionResult
    def register_custom_handler(name, handler)
```

### 1.3 依赖

- `apscheduler`：时间调度
- `simpleeval`：条件表达式求值
- `jinja2`：模板渲染
- `httpx`：HTTP 请求
- `src.core.event_bus`：事件总线

---

## 二、逻辑

### 2.1 触发器类型

| 类型 | 说明 | 触发条件 |
|---|---|---|
| TIME | 时间触发器 | Cron 表达式、间隔、单次 |
| EVENT | 事件触发器 | 系统事件类型匹配 |
| CONDITION | 条件触发器 | 条件表达式求值为真 |

### 2.2 数据模型

#### TriggerType（触发器类型）
| 值 | 说明 |
|---|---|
| TIME | 时间触发器 |
| EVENT | 事件触发器 |
| CONDITION | 条件触发器 |

#### ActionType（动作类型）
| 值 | 说明 |
|---|---|
| NOTIFICATION | 通知 |
| API_CALL | API 调用 |
| TASK_RETRY | 任务重试 |
| TASK_COMPLETE | 任务完成 |
| CUSTOM | 自定义 |

#### TriggerStatus（触发器状态）
| 值 | 说明 |
|---|---|
| ENABLED | 已启用 |
| DISABLED | 已禁用 |
| RUNNING | 运行中 |
| ERROR | 错误 |

#### TriggerConfig（触发器配置）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 触发器 ID |
| name | str | 触发器名称 |
| trigger_type | TriggerType | 触发器类型 |
| enabled | bool | 是否启用 |
| description | str | None | 描述 |
| actions | list[ActionConfig] | 动作列表 |
| metadata | dict | 元数据 |
| schedule | dict | None | 时间触发器配置 |
| event | dict | None | 事件触发器配置 |
| condition | dict | None | 条件触发器配置 |

#### ActionConfig（动作配置）
| 字段 | 类型 | 说明 |
|---|---|---|
| type | ActionType | 动作类型 |
| config | dict | 动作配置 |
| order | int | 执行顺序 |

#### ExecutionResult（执行结果）
| 字段 | 类型 | 说明 |
|---|---|---|
| success | bool | 是否成功 |
| message | str | 消息 |
| data | dict | 数据 |
| error | str | None | 错误信息 |
| executed_at | datetime | 执行时间 |

### 2.3 执行流程

```
┌─────────────────────────────────────────────────────────────┐
│                      事件触发流程                            │
├─────────────────────────────────────────────────────────────┤
│  1. 事件总线发布事件                                         │
│  2. TriggerManager 接收事件                                  │
│  3. 查找匹配的触发器                                         │
│  4. 检查触发器状态                                           │
│  5. 执行触发器动作                                           │
│  6. 返回执行结果                                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      时间触发流程                            │
├─────────────────────────────────────────────────────────────┤
│  1. APScheduler 触发                                         │
│  2. TimeTrigger.execute() 被调用                            │
│  3. 执行配置的动作                                           │
│  4. 返回执行结果                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.4 动作执行器

动作执行器负责执行各类动作：

| 动作类型 | 说明 |
|---|---|
| NOTIFICATION | 发送通知（WebSocket、数据库、Webhook） |
| API_CALL | 调用 API |
| TASK_RETRY | 重试任务 |
| TASK_COMPLETE | 标记任务完成 |
| CUSTOM | 自定义处理器 |

### 2.5 错误处理

- 触发器禁用：跳过执行
- 条件不满足：返回失败结果
- 动作执行失败：记录错误，继续执行后续动作（可配置停止）

---

## 三、结构

### 3.1 组件清单

| 组件 | 职责 |
|---|---|
| TriggerManager | 触发器管理器，管理触发器生命周期 |
| TriggerRegistry | 触发器注册表，从配置加载触发器 |
| TriggerStateManager | 触发器状态管理器 |
| BaseTrigger | 触发器基类 |
| TimeTrigger | 时间触发器 |
| EventTrigger | 事件触发器 |
| ConditionTrigger | 条件触发器 |
| ActionExecutor | 动作执行器 |

### 3.2 文件清单

| 文件 | 说明 |
|---|---|
| `__init__.py` | 模块导出 |
| `models.py` | 数据模型定义 |
| `manager.py` | 触发器管理器 |
| `registry.py` | 触发器注册表 |
| `state_manager.py` | 触发器状态管理器 |
| `triggers/base.py` | 触发器基类 |
| `triggers/time_trigger.py` | 时间触发器 |
| `triggers/event_trigger.py` | 事件触发器 |
| `triggers/condition_trigger.py` | 条件触发器 |
| `actions/executor.py` | 动作执行器 |

### 3.3 测试策略

- 单元测试：测试各触发器的触发逻辑
- 集成测试：测试触发器与事件总线的集成
- 边界测试：测试条件表达式求值、时间触发器边界情况
