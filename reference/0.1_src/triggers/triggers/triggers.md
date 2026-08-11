# 触发器类型组件

## 需求
### 职责
定义各类触发器的实现，包括事件触发器、条件触发器和时间触发器，在满足条件时执行配置的动作。

### 对外接口
- 输入：触发器配置（TriggerConfig）、事件数据
- 输出：执行结果（ExecutionResult）

### 依赖
- 依赖模块：src.triggers.models（触发器模型）
- 依赖模块：src.triggers.actions.executor（动作执行器）
- 依赖模块：src.core.event_bus.types（事件类型）
- 依赖模块：apscheduler（时间调度）
- 依赖模块：simpleeval（表达式求值）

## 逻辑
### 流程设计
1. 初始化触发器配置
2. 接收触发事件/条件
3. 检查是否满足触发条件
4. 执行配置的动作

### 数据流向
```
TriggerConfig → BaseTrigger → execute → execute_actions → ExecutionResult
```

### 错误处理
- 条件表达式求值失败时返回 False（不触发）
- 记录执行日志便于排查

### 安全设计
- 使用 simpleeval 安全评估表达式，防止代码注入
- 表达式求值在受限环境中执行

## 结构
### 子组件清单（文件夹 - 抽象说明）
| 子组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| base | 触发器抽象基类 | 输入：配置 → 输出：执行结果 | - |
| event_trigger | 事件触发器 | 输入：事件 → 输出：执行结果 | - |
| condition_trigger | 条件触发器 | 输入：事件 → 输出：执行结果 | - |
| time_trigger | 时间触发器 | 输入：时间调度 → 输出：执行结果 | - |

### 文件清单（代码文件 - 具体接口）
#### base.py
职责：触发器抽象基类
暴露接口：
- `BaseTrigger`：触发器抽象基类
  - `__init__(config: TriggerConfig)`：初始化
  - `async execute(*args, **kwargs) -> ExecutionResult`：执行触发器（抽象方法）
  - `async execute_actions(context: dict[str, Any] | None = None) -> ExecutionResult`：执行所有配置动作
  - `validate() -> bool`：验证触发器配置
  - `to_dict() -> dict[str, Any]`：转换为字典

#### event_trigger.py
职责：事件触发器，监听特定类型的系统事件
暴露接口：
- `EventTrigger`：事件触发器类
  - `__init__(config: TriggerConfig)`：初始化
  - `async execute(event: ExecutionEvent) -> ExecutionResult`：处理事件
  - `matches_event(event_type: str) -> bool`：检查是否监听指定事件类型

#### condition_trigger.py
职责：条件触发器，基于条件表达式判断是否触发
暴露接口：
- `ConditionTrigger`：条件触发器类
  - `__init__(config: TriggerConfig)`：初始化
  - `async execute(event: ExecutionEvent) -> ExecutionResult`：处理事件
  - `get_event_history(limit: int = 10) -> list[dict[str, Any]]`：获取事件历史
  - `clear_history() -> None`：清空历史
  - `matches_event(event_type: str) -> bool`：检查是否监听指定事件类型

#### time_trigger.py
职责：时间触发器，基于时间调度执行
暴露接口：
- `TimeTrigger`：时间触发器类
  - `__init__(config: TriggerConfig)`：初始化
  - `async execute(*args, **kwargs) -> ExecutionResult`：执行触发器
  - `get_apscheduler_trigger()`：获取 APScheduler 触发器对象
  - `get_next_run_time() -> datetime | None`：获取下次运行时间

  支持的调度类型：
  - cron：Cron 表达式
  - interval：固定间隔
  - date：单次执行

### 测试策略
#### 组件测试
- 单元测试：各触发器的条件判断逻辑
- 集成测试：触发器与动作执行器的集成
- Mock策略：事件数据 Mock、时间 Mock

## 实现
→ 见代码文件
