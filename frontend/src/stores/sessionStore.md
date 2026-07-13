# useSessionStore

## 用途

Zustand 会话状态管理 Store，是前端最复杂的 Store。负责管理消息列表、流式响应、消息 CRUD（增删改）、分页加载、WebSocket 通信、消息版本管理等核心业务逻辑。

## API

### 状态

| 属性 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `sessions` | `Session[]` | `[]` | 所有会话列表 |
| `activeSessionId` | `string \| null` | `null` | 当前活跃会话 ID |
| `messages` | `Record<string, Message[]>` | `{}` | 按会话 ID 索引的消息字典 |
| `isLoading` | `boolean` | `false` | 是否正在加载 |
| `deletingSessionIds` | `Set<string>` | `new Set()` | 正在删除的会话 ID 集合 |
| `error` | `string \| null` | `null` | 最近错误信息 |
| `wsStatus` | `string` | `DISCONNECTED` | WebSocket 连接状态 |
| `forceReconnect` | `boolean` | `false` | 是否需要强制重连 |
| `messagePagination` | `Record<string, PaginationState>` | `{}` | 按会话 ID 索引的分页状态 |

### 消息操作方法

#### `addMessage(sessionId: string, message: Message): void`

添加或更新消息。如果消息已存在（按 ID 匹配）则更新，否则追加。添加后按 sequence 和 timestamp 排序。

- 同时更新对应会话的 `messageCount` 和 `updatedAt`

#### `updateMessageContent(sessionId, messageId, content, options?): void`

更新消息内容。支持两种模式：

| 模式 | 说明 |
|------|------|
| `append`（默认） | 追加内容到已有内容末尾（流式场景） |
| `replace` | 替换全部内容 |

#### `updateMessageFields(sessionId, messageId, updates): void`

更新消息的部分字段（浅合并）。

#### `deleteMessage(sessionId, messageId, includeTarget?): void`

删除消息。采用**乐观更新**策略：

1. 前端立即删除消息（包括所有子消息的递归删除）
2. 调用后端 API 确认删除
3. 成功后重新加载消息保证一致性
4. 失败时回滚到之前的状态

- `includeTarget=true`：删除目标及之后的所有同级消息
- `includeTarget=false`：仅删除目标之后的消息

#### `deleteMessageFromList(sessionId, messageId, deletedCount?): void`

从前端消息列表中移除消息（不调用后端 API）。用于后端推送的删除通知。

### 查询方法

#### `getActiveSessionMessages(): Message[]`

获取当前活跃会话的消息列表。无活跃会话时返回空数组。

### 加载方法

#### `fetchMessages(sessionId, options?): Promise<void>`

从后端加载消息。支持分页参数。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `skip` | `0` | 跳过的消息数 |
| `limit` | `20` | 每页加载条数 |
| `append` | `false` | 是否追加到已有消息前（加载更多历史） |

- 临时会话（`temp-` 开头）跳过加载
- append 模式：历史消息插入到已有消息前
- 非 append 模式：与本地独有消息合并

#### `fetchSubMessages(sessionId, parentId): Promise<void>`

加载指定消息的子消息。如果已加载过则跳过。

#### `loadMoreMessages(sessionId): Promise<void>`

加载更多历史消息。根据分页状态自动计算 offset。

### WebSocket 方法

#### `connectWebSocket(sessionId, token): void`

建立 WebSocket 连接，订阅消息相关事件。

#### `disconnectWebSocket(): void`

断开 WebSocket 连接。

### 其他方法

#### `retryMessage(sessionId, messageId, scope?, targetToolId?): Promise<void>`

重试消息。支持不同重试范围（scope）。

#### `createMessageVersion(sessionId, messageId): number`

创建消息版本。返回新版本号。

#### `restoreMessageVersion(sessionId, messageId, version): void`

恢复到指定消息版本。

#### `getMessageVersions(sessionId, messageId): MessageVersion[]`

获取消息的所有版本列表。

#### `clearError(): void`

清除错误状态。

## 使用示例

```tsx
import { useSessionStore } from '@/stores/sessionStore'

function ChatPage({ sessionId }: { sessionId: string }) {
  const messages = useSessionStore((s) => s.messages[sessionId] || [])
  const isLoading = useSessionStore((s) => s.isLoading)
  const fetchMessages = useSessionStore((s) => s.fetchMessages)
  const loadMoreMessages = useSessionStore((s) => s.loadMoreMessages)
  const pagination = useSessionStore((s) => s.getMessagePagination(sessionId))

  useEffect(() => {
    fetchMessages(sessionId)
  }, [sessionId])

  return (
    <div>
      {pagination.hasMore && (
        <button onClick={() => loadMoreMessages(sessionId)}>
          加载更多
        </button>
      )}
      {messages.map((msg) => (
        <MessageItem key={msg.id} message={msg} />
      ))}
    </div>
  )
}
```

## 依赖关系

| 依赖 | 类型 | 说明 |
|------|------|------|
| `zustand` | 状态管理 | Store 创建（不使用 persist） |
| `messageApi` | API 服务 | 消息 CRUD 后端 API |
| `sessionApi.getMessages` | API 服务 | 获取会话消息 |
| `webSocketService` | 服务 | WebSocket 通信 |
| `useMessageVersionStore` | Zustand Store | 消息版本管理 |
| `loggers.sessionStore` | 工具 | 结构化日志 |

### 消息排序规则

消息按 `sequence` 升序排列，相同 sequence 按 `timestamp` 升序排列。

## 注意事项

1. **乐观更新**：`deleteMessage` 先更新前端再调用后端，失败时回滚
2. **临时会话**：以 `temp-` 开头的会话 ID 跳过消息加载
3. **分页状态**：每个会话独立维护分页状态（offset、hasMore、isLoadingMore）
4. **消息合并**：fetchMessages 在非 append 模式下会保留本地独有的消息（如乐观添加的用户消息）
5. **递归删除**：`deleteMessage` 会递归查找并删除所有子消息
6. **WebSocket 状态**：`wsStatus` 和 `forceReconnect` 用于管理连接生命周期
7. **不持久化**：会话和消息数据不使用 persist，每次刷新重新从后端加载
