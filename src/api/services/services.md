# Services 组件

## 需求

### 职责
提供 API 层的业务服务实现，处理聊天、通知、数据库监听等核心功能。

### 对外接口
- 输入：业务请求参数、数据库会话
- 输出：业务处理结果

### 依赖
- `src/agents`：Agent 执行引擎
- `src/memory`：记忆系统
- `src/db`：数据库访问层
- `src/tools`：工具系统

## 逻辑

### 流程设计

#### ChatService 流程
```
用户输入 → 记忆增强 → Agent 执行 → 流式输出 → 结果返回
```

#### NotificationService 流程
```
创建通知 → 存储数据库 → WebSocket 推送 → 标记已读
```

#### PostgresListener 流程
```
连接数据库 → 监听通道 → 接收通知 → 广播事件
```

### 数据流向
1. 路由层接收请求，调用服务层方法
2. 服务层获取必要依赖（数据库会话、记忆服务）
3. 执行业务逻辑（调用 Agent、存储数据）
4. 返回处理结果给路由层

### API设计

#### ChatService 方法
| 方法 | 用途 | 参数 | 返回值 |
|------|------|------|--------|
| `enhance_user_input_with_memory` | 记忆增强用户输入 | user_input, user_id, session_id | dict[str, Any] |
| `summarize_context` | 生成上下文摘要 | context | str |
| `search_memories` | 搜索记忆 | user_id, session_id, query | dict[str, Any] |
| `create_episode_memory` | 创建情景记忆 | user_id, session_id, intent_text | dict[str, Any] |
| `get_memory_stats` | 获取记忆统计 | user_id, session_id | dict[str, Any] |
| `stream_response` | 流式生成 AI 响应 | db, thread_id, user_input | AsyncIterator[str] |

#### NotificationService 方法
| 方法 | 用途 | 参数 | 返回值 |
|------|------|------|--------|
| `create_notification` | 创建通知 | user_id, title, message, type, priority | dict |
| `get_unread_notifications` | 获取未读通知 | user_id, limit | list[dict] |
| `get_unpushed_notifications` | 获取未推送通知 | user_id, limit | list[dict] |
| `get_user_notifications` | 获取用户通知列表 | user_id, limit, offset, unread_only | list[dict] |
| `mark_as_read` | 标记已读 | notification_id, user_id | bool |
| `mark_as_pushed` | 标记已推送 | notification_id | bool |
| `mark_multiple_as_read` | 批量标记已读 | notification_ids | int |
| `count_unread_notifications` | 统计未读数量 | user_id | int |
| `get_notification_stats` | 获取通知统计 | user_id | dict |
| `cleanup_old_notifications` | 清理旧通知 | user_id, days | int |

#### PostgresNotifier 方法
| 方法 | 用途 | 参数 | 返回值 |
|------|------|------|--------|
| `connect` | 连接数据库 | - | None |
| `disconnect` | 断开连接 | - | None |
| `add_listener` | 添加监听器 | callback | None |
| `listen_to_channel` | 监听通道 | channel | None |
| `start_listening` | 启动监听循环 | - | None |

### 配置设计
- ChatService 使用全局会话管理器
- NotificationService 支持依赖注入数据库会话
- PostgresNotifier 需要数据库连接 URL

### 错误处理
- 服务层捕获异常并记录日志
- 返回包含错误信息的字典
- 数据库操作失败时自动回滚

## 结构

### 文件清单

#### `chat_service.py`
职责：聊天服务，集成记忆增强和 Agent 执行
暴露接口：
- `ChatService`：聊天服务类
  - `enhance_user_input_with_memory(user_input, user_id, session_id, ...) -> dict[str, Any]`：记忆增强
  - `summarize_context(context) -> str`：上下文摘要
  - `search_memories(user_id, session_id, query, ...) -> dict[str, Any]`：搜索记忆
  - `create_episode_memory(user_id, session_id, intent_text, ...) -> dict[str, Any]`：创建情景记忆
  - `get_memory_stats(user_id, session_id) -> dict[str, Any]`：记忆统计
  - `stream_response(db, thread_id, user_input, ...) -> AsyncIterator[str]`：流式响应
- `get_chat_service() -> ChatService`：获取全局服务实例

#### `notification_service.py`
职责：通知服务，管理系统通知
暴露接口：
- `NotificationService(BaseService)`：通知服务类
  - `create_notification(user_id, title, message, ...) -> dict`：创建通知
  - `get_unread_notifications(user_id, limit) -> list[dict]`：获取未读通知
  - `get_unpushed_notifications(user_id, limit) -> list[dict]`：获取未推送通知
  - `get_user_notifications(user_id, limit, offset, ...) -> list[dict]`：获取用户通知
  - `mark_as_read(notification_id, user_id) -> bool`：标记已读
  - `mark_as_pushed(notification_id) -> bool`：标记已推送
  - `mark_multiple_as_read(notification_ids) -> int`：批量标记已读
  - `count_unread_notifications(user_id) -> int`：统计未读数量
  - `get_notification_stats(user_id) -> dict`：获取统计
  - `cleanup_old_notifications(user_id, days) -> int`：清理旧通知
- `create_notification_service() -> NotificationService`：创建服务实例
- `create_system_notification(user_id, title, message, ...) -> dict`：创建系统通知

#### `postgres_listener.py`
职责：PostgreSQL LISTEN/NOTIFY 监听器
暴露接口：
- `PostgresNotifier`：PostgreSQL 通知监听器类
  - `connect() -> None`：连接数据库
  - `disconnect() -> None`：断开连接
  - `add_listener(callback) -> None`：添加事件监听器
  - `listen_to_channel(channel) -> None`：监听指定通道
  - `start_listening() -> None`：启动监听循环

### 测试策略
- 单元测试：服务方法逻辑、异常处理
- 集成测试：数据库交互、Agent 调用
- Mock 策略：Mock 数据库会话、Agent 实例

## 实现
→ 见代码文件 `src/api/services/*.py`
