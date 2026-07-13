# 事件总线组件

## 需求
### 职责
提供分布式事件发布/订阅机制，支持跨进程事件通信、消息持久化和消费者组负载均衡。

### 对外接口
- 输入：事件对象、订阅处理器、过滤器
- 输出：事件 ID、订阅 ID、事件历史

### 依赖
- 外部依赖：redis（可选，用于 Redis Streams 实现）
- 内部依赖：src.config.settings（配置读取）

## 逻辑
### 流程设计
1. **连接管理**：建立/断开与后端存储的连接
2. **事件发布**：将事件发布到主流和会话流
3. **事件订阅**：注册处理器，支持过滤器和消费者组
4. **事件消费**：从流中读取事件并分发给处理器

### 数据流向
```
发布者 -> publish(event) -> Redis Stream / 内存队列 -> 订阅者处理器
```

### 数据模型
#### EventType（事件类型枚举）
| 值 | 说明 |
|---|---|
| STATE_CHANGE | 状态变更 |
| STEP_START/COMPLETE/ERROR | 步骤生命周期 |
| APPROVAL_REQUEST/RESPONSE | 审批流程 |
| EXECUTION_START/COMPLETE/ERROR | 执行生命周期 |
| TOOL_CALL_START/END | 工具调用 |
| STREAM_CHUNK | 流式输出 |
| TASK_SUBMITTED/EXECUTION_REQUESTED | 任务事件 |

#### ExecutionEvent（执行事件）
| 字段 | 类型 | 说明 |
|---|---|---|
| event_id | str | 事件唯一 ID |
| event_type | EventType | 事件类型 |
| session_id | str | 会话 ID |
| data | dict | 事件数据 |
| timestamp | datetime | 时间戳 |
| metadata | dict | 元数据 |
| priority | EventPriority | 优先级 |
| source | str \| None | 事件来源（进程/实例标识） |

#### EventFilter（事件过滤器）
| 字段 | 类型 | 说明 |
|---|---|---|
| event_types | list[EventType] \| None | 事件类型过滤 |
| session_ids | list[str] \| None | 会话 ID 过滤 |
| min_priority | EventPriority \| None | 最低优先级 |
| sources | list[str] \| None | 事件来源过滤 |
| custom_event_types | list[str] \| None | 自定义事件类型过滤 |

### 配置设计
| 配置项 | 说明 | 默认值 |
|---|---|---|
| event_bus_type | 事件总线类型 | redis_streams |
| redis_url | Redis 连接 URL | 从 settings 读取 |

### 错误处理
- 发布失败自动重试（指数退避）
- 失败事件发送到死信队列
- Redis 连接失败降级到内存模式

## 结构
### 子组件清单
无

### 文件清单（代码文件 - 具体接口）
#### base.py
职责：事件总线抽象基类
暴露接口：
- `EventBusBase`：事件总线抽象基类
  - `connect() -> None`：建立连接
  - `disconnect() -> None`：断开连接
  - `publish(event: ExecutionEvent, retry_count: int) -> str`：发布事件
  - `subscribe(handler: EventHandler, filter: EventFilter, consumer_group: str) -> str`：订阅事件
  - `unsubscribe(subscription_id: str) -> bool`：取消订阅
  - `get_history(session_id: str, event_type: EventType, limit: int) -> list[ExecutionEvent]`：获取历史
  - `get_metrics() -> dict`：获取指标
  - `health_check() -> dict`：健康检查
  - `emit_state_change(session_id: str, old_state: str, new_state: str) -> str`：发送状态变更
  - `emit_step_start/complete/error(...)`：发送步骤事件
  - `emit(event_type: str, data: dict, session_id: str) -> str`：通用发送

#### types.py
职责：事件类型定义
暴露接口：
- `EventType`：事件类型枚举
- `EventPriority`：事件优先级枚举
- `ExecutionEvent`：执行事件模型
- `EventFilter`：事件过滤器
- `EventHandler`：事件处理器类型
- `Subscription`：订阅信息

#### factory.py
职责：事件总线工厂
暴露接口：
- `EventBusType`：事件总线类型枚举
- `create_event_bus(bus_type: EventBusType, redis_url: str, **kwargs) -> EventBusBase`：创建事件总线
- `get_event_bus(bus_type: EventBusType, redis_url: str, **kwargs) -> EventBusBase`：获取全局单例
- `reset_event_bus() -> None`：重置全局实例
- `shutdown_event_bus() -> None`：关闭全局实例

#### memory.py
职责：内存事件总线实现
暴露接口：
- `InMemoryEventBus`：内存事件总线类
  - 继承 EventBusBase 所有方法
  - `clear_history() -> None`：清除历史
  - `get_stats() -> dict`：获取统计

#### redis_streams.py
职责：Redis Streams 事件总线实现
暴露接口：
- `RedisStreamsEventBus`：Redis Streams 事件总线类
  - 继承 EventBusBase 所有方法
  - `acknowledge(event_id: str, consumer_group: str) -> bool`：确认消息
  - `get_pending_events(consumer_group: str, consumer_name: str) -> list[ExecutionEvent]`：获取待处理
  - `get_dead_letter_events(limit: int) -> list[ExecutionEvent]`：获取死信队列
  - `retry_dead_letter_event(event_id: str) -> bool`：重试死信
  - `claim_stale_messages(consumer_group: str, min_idle_time: int) -> list[ExecutionEvent]`：认领超时消息

### 测试策略
#### 组件测试
- 单元测试：事件模型、过滤器匹配
- 集成测试：发布/订阅流程、消费者组
- 覆盖率要求：核心逻辑 >= 85%

## 实现
-> 见代码文件：src/core/event_bus/
