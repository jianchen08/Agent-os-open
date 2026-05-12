# Handlers 组件

## 需求

### 职责
处理 WebSocket 消息，实现用户输入、控制命令、交互响应等消息的处理逻辑。

### 对外接口
- 输入：消息数据、处理器上下文
- 输出：处理结果（可选）

### 依赖
- `src/api/websocket/handlers/components`：处理器子组件
- `src/agents`：Agent 执行引擎
- `src/db`：数据库访问层

## 逻辑

### 流程设计

#### 消息处理流程
```
接收消息 → 匹配处理器 → 验证消息 → 执行处理 → 返回结果
```

#### 用户输入处理流程
```
验证格式 → 保存用户消息 → 发送 stream_start → Agent 执行 → 发送 stream_end
```

### 数据流向
1. MessageDispatcher 根据消息类型选择处理器
2. 处理器验证消息格式
3. 处理器调用子组件执行业务逻辑
4. 处理器通过 MessageBus 推送事件

### API设计

#### 消息类型与处理器映射
| 消息类型 | 处理器 | 用途 |
|----------|--------|------|
| `user_input` | UserInputHandler | 用户聊天消息 |
| `regenerate` | RegenerateHandler | 重新生成消息 |
| `stop_generation` | ControlHandler | 停止生成 |
| `resume_action` | ControlHandler | 恢复操作 |
| `heartbeat` | ControlHandler | 心跳 |
| `execution_control` | ControlHandler | 执行控制 |
| `interaction_response` | InteractionResponseHandler | 交互响应 |
| `conversation_message` | ConversationMessageHandler | 对话消息 |

### 错误处理
- 消息格式错误返回错误事件
- 处理异常记录日志并返回错误
- 数据库操作失败自动回滚

## 结构

### 子组件清单

| 子组件 | 职责 | 对外接口 | 文档 |
|--------|------|----------|------|
| components | 处理器子组件 | 输入：消息数据 → 输出：处理结果 | components/components.md |

### 文件清单

#### `__init__.py`
职责：处理器模块入口，导出所有处理器
暴露接口：
- `BaseHandler`：处理器基类
- `HandlerContext`：处理器上下文
- `UserInputHandler`：用户输入处理器
- `RegenerateHandler`：重新生成处理器
- `ControlHandler`：控制命令处理器
- `InteractionResponseHandler`：交互响应处理器
- `ConversationMessageHandler`：对话消息处理器

#### `base.py`
职责：定义处理器基类和上下文
暴露接口：
- `HandlerContext(dataclass)`：处理器上下文
  - `websocket: WebSocket`：WebSocket 连接
  - `thread_id: str`：线程 ID
  - `user_id: str`：用户 ID
  - `db: AsyncSession`：数据库会话
  - `agent_loop: AgentLoop`：Agent 循环
  - `agent_config: Any`：Agent 配置
- `BaseHandler(ABC)`：处理器基类
  - `handle(ctx, data) -> dict | None`：处理消息（抽象方法）
  - `can_handle(message_type) -> bool`：判断是否能处理（抽象方法）

#### `user_input.py`
职责：处理用户输入消息
暴露接口：
- `UserInputHandler(BaseHandler)`：用户输入处理器
  - `can_handle(message_type) -> bool`：判断是否能处理
  - `handle(ctx, data) -> dict | None`：处理用户输入
  - `_send_assistant_message(...) -> None`：发送助手消息

#### `control.py`
职责：处理控制命令（停止、恢复、心跳）
暴露接口：
- `ControlHandler(BaseHandler)`：控制命令处理器
  - `can_handle(message_type) -> bool`：判断是否能处理
  - `handle(ctx, data) -> dict | None`：处理控制命令
  - `_handle_stop(ctx, data) -> dict | None`：处理停止生成
  - `_handle_resume(ctx, data) -> dict | None`：处理恢复操作
  - `_handle_heartbeat(ctx, data) -> dict | None`：处理心跳
  - `_handle_execution_control(ctx, data) -> dict | None`：处理执行控制
- `_update_execution_record_status(db, execution_id, new_status, action) -> bool`：更新执行记录状态

#### `interaction.py`
职责：处理交互响应和对话消息
暴露接口：
- `InteractionResponseHandler(BaseHandler)`：交互响应处理器
  - `can_handle(message_type) -> bool`：判断是否能处理
  - `handle(ctx, data) -> dict | None`：处理交互响应
- `ConversationMessageHandler(BaseHandler)`：对话消息处理器
  - `can_handle(message_type) -> bool`：判断是否能处理
  - `handle(ctx, data) -> dict | None`：处理对话消息

#### `regenerate.py`
职责：处理消息重新生成
暴露接口：
- `RegenerateHandler(BaseHandler)`：重新生成处理器
  - `can_handle(message_type) -> bool`：判断是否能处理
  - `handle(ctx, data) -> dict | None`：处理重新生成请求

### 测试策略
- 单元测试：各处理器的消息处理逻辑
- 集成测试：完整消息处理流程
- Mock 策略：Mock Agent 循环、数据库会话

## 实现
→ 见代码文件 `src/api/websocket/handlers/*.py`
