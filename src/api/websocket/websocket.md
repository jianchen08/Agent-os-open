# WebSocket 组件

## 需求

### 职责
提供 WebSocket 实时通信功能，包括连接管理、消息分发、事件推送等核心能力。

### 对外接口
- 输入：WebSocket 连接、客户端消息
- 输出：实时事件推送、流式响应

### 依赖
- FastAPI WebSocket
- `src/agents`：Agent 执行引擎
- `src/db`：数据库访问层
- `src/api/websocket/handlers`：消息处理器

## 逻辑

### 流程设计

#### 连接管理流程
```
客户端连接 → 认证验证 → 注册连接 → 消息循环 → 断开清理
```

#### 消息处理流程
```
接收消息 → 解析类型 → 分发处理器 → 执行处理 → 返回结果
```

#### 事件推送流程
```
业务事件 → 创建事件对象 → 连接管理器推送 → 客户端接收
```

### 数据流向
1. 客户端建立 WebSocket 连接
2. ConnectionManager 注册连接并关联线程/用户
3. 客户端发送消息，MessageDispatcher 分发到对应处理器
4. 处理器执行业务逻辑，通过 MessageBus 推送事件
5. 连接断开时，ConnectionManager 清理资源

### API设计

#### 连接管理方法
| 方法 | 用途 | 参数 | 返回值 |
|------|------|------|--------|
| `connect` | 接受新连接 | websocket, thread_id, user_id | bool |
| `disconnect` | 断开连接 | websocket, thread_id | None |
| `send_to_connection` | 发送到单个连接 | websocket, message | bool |
| `send_to_thread` | 发送到线程所有连接 | thread_id, message | int |
| `send_to_user` | 发送到用户所有连接 | user_id, message | int |
| `broadcast` | 广播到所有连接 | message | int |

#### 事件服务方法
| 方法 | 用途 | 参数 |
|------|------|------|
| `send_task_created` | 推送任务创建事件 | user_id, taskId, goal, taskType, phase |
| `send_task_phase_changed` | 推送阶段变更事件 | user_id, taskId, phase, status, timestamp |
| `send_task_completed` | 推送任务完成事件 | user_id, taskId, result, summary |
| `send_task_failed` | 推送任务失败事件 | user_id, taskId, error, retryCount |
| `send_execution_start` | 推送执行开始事件 | user_id, execution_id, execution_type, name |
| `send_execution_progress` | 推送执行进度事件 | user_id, execution_id, progress, message |
| `send_execution_done` | 推送执行完成事件 | user_id, execution_id, success, output |
| `send_thinking_start` | 推送思考开始事件 | user_id, execution_id, model |
| `send_thinking_chunk` | 推送思考内容片段 | user_id, execution_id, chunk |
| `send_thinking_end` | 推送思考结束事件 | user_id, execution_id, duration_ms |

### 事件类型

#### 任务生命周期事件
| 事件类型 | 用途 |
|----------|------|
| `task_created` | 任务创建 |
| `task_phase_changed` | 阶段变更 |
| `task_ac_evaluated` | AC 评估完成 |
| `task_completed` | 任务完成 |
| `task_failed` | 任务失败 |

#### 执行事件
| 事件类型 | 用途 |
|----------|------|
| `execution_start` | 执行开始 |
| `execution_progress` | 执行进度 |
| `execution_done` | 执行完成 |
| `execution_cancelled` | 执行取消 |

#### 思考模式事件
| 事件类型 | 用途 |
|----------|------|
| `thinking_start` | 思考开始 |
| `thinking_chunk` | 思考内容片段 |
| `thinking_end` | 思考结束 |

#### 用户交互事件
| 事件类型 | 用途 |
|----------|------|
| `clarification_needed` | 需要澄清 |
| `interaction_requested` | 交互请求 |

### 错误处理
- 连接异常自动清理资源
- 消息发送失败自动重试（最多 3 次）
- 错误码规范：定义在 `error_codes.py`

### 安全设计
- 连接时验证用户身份
- 消息处理前验证线程归属
- 敏感操作需要用户确认

## 结构

### 子组件清单

| 子组件 | 职责 | 对外接口 | 文档 |
|--------|------|----------|------|
| handlers | 消息处理器 | 输入：消息数据 → 输出：处理结果 | handlers/handlers.md |

### 文件清单

#### `__init__.py`
职责：WebSocket 模块入口，导出事件类型和连接管理器
暴露接口：
- 事件类型：`TokenStreamEvent`, `StateChangeEvent`, `TaskCreatedEvent` 等
- 工厂函数：`create_token_stream_event`, `create_task_created_event` 等
- 连接管理：`ConnectionManager`
- 错误码：`WebSocketErrorCode`, `ERROR_MESSAGES`

#### `handler.py`
职责：WebSocket 连接管理器
暴露接口：
- `ConnectionManager`：连接管理器类
  - `connect(websocket, thread_id, user_id) -> bool`：接受连接
  - `disconnect(websocket, thread_id) -> None`：断开连接
  - `send_to_connection(websocket, message) -> bool`：发送到单个连接
  - `send_to_thread(thread_id, message) -> int`：发送到线程
  - `send_to_user(user_id, message) -> int`：发送到用户
  - `broadcast(message) -> int`：广播
  - `get_connection_count() -> int`：获取连接数
  - `send_tool_call_start(...)`：发送工具调用开始
  - `send_tool_call_end(...)`：发送工具调用结束
- `connection_manager: ConnectionManager`：全局连接管理器实例

#### `service.py`
职责：WebSocket 事件推送服务
暴露接口：
- `WebSocketEventService`：事件推送服务类
  - `send_task_created(...) -> bool`：推送任务创建
  - `send_task_phase_changed(...) -> bool`：推送阶段变更
  - `send_task_completed(...) -> bool`：推送任务完成
  - `send_execution_start(...) -> bool`：推送执行开始
  - `send_execution_progress(...) -> bool`：推送执行进度
  - `send_execution_done(...) -> bool`：推送执行完成
  - `send_thinking_start(...) -> bool`：推送思考开始
  - `send_thinking_chunk(...) -> bool`：推送思考片段
  - `send_thinking_end(...) -> bool`：推送思考结束
- `get_event_service() -> WebSocketEventService`：获取全局服务实例

#### `dispatcher.py`
职责：消息分发器
暴露接口：
- `MessageDispatcher`：消息分发器类
  - `register_handler(handler) -> None`：注册处理器
  - `get_handler(data) -> BaseHandler | None`：获取处理器
  - `dispatch(ctx, data) -> dict | None`：分发消息
- `message_dispatcher: MessageDispatcher`：全局分发器实例

#### `events.py`
职责：事件类型定义
暴露接口：
- 事件类：`TokenStreamEvent`, `TaskCreatedEvent`, `ExecutionStartEvent` 等
- 工厂函数：`create_*_event()` 系列

#### `error_codes.py`
职责：错误码定义
暴露接口：
- `WebSocketErrorCode(Enum)`：错误码枚举
- `ERROR_MESSAGES: dict`：错误消息映射
- `get_error_message(code) -> str`：获取错误消息
- `is_retryable_error(code) -> bool`：判断是否可重试

### 测试策略
- 单元测试：连接管理、消息分发、事件创建
- 集成测试：完整消息流程、事件推送
- 并发测试：多连接、并发消息处理

## 实现
→ 见代码文件 `src/api/websocket/*.py`
