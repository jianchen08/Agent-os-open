# useMessageActions

## 用途

消息操作自定义 Hook，封装消息的编辑、删除、重试（支持部分重试）以及版本管理功能。内部集成 `toast` 提示和错误上报，提供开箱即用的消息操作体验。

适用于聊天界面的消息上下文菜单、消息操作按钮等场景。

## API

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| sessionId | `string` | 否 | `undefined` | 当前会话 ID。为空时调用任何方法将抛出错误 |

### 返回值

| 方法 | 类型 | 说明 |
|------|------|------|
| editMessage | `(messageId: string, newContent: string) => Promise<unknown>` | 编辑消息内容 |
| deleteMessage | `(messageId: string, includeTarget?: boolean) => Promise<void>` | 删除消息 |
| retryMessageWithScope | `(messageId: string, scope?: RetryScope, targetToolId?: string) => Promise<void>` | 按范围重试消息 |
| createMessageVersion | `(messageId: string) => number` | 创建消息版本快照 |
| restoreMessageVersion | `(messageId: string, version: number) => void` | 恢复到指定版本 |
| getMessageVersions | `(messageId: string) => Promise<unknown[]>` | 获取消息版本列表 |

### 方法详情

#### editMessage

编辑指定消息的内容。

- **参数**：
  - `messageId: string` — 消息 ID
  - `newContent: string` — 新的消息内容
- **返回**：`Promise<unknown>` — API 返回的编辑结果
- **说明**：成功时 toast 提示"消息已更新"，失败时 toast 提示"编辑消息失败"并上报错误。

#### deleteMessage

删除指定消息及其后续消息。

- **参数**：
  - `messageId: string` — 消息 ID
  - `includeTarget: boolean` — 是否包含目标消息本身，默认 `true`。`true` 表示删除当前消息及之后的所有消息，`false` 表示仅删除当前消息之后的消息
- **返回**：`Promise<void>`
- **说明**：内部调用 `sessionStore.deleteMessage`（乐观更新 + API 调用）。成功时 toast 提示"消息已删除"。

#### retryMessageWithScope

按范围重试消息生成，支持全部重试、仅重试失败工具、重试特定工具。

- **参数**：
  - `messageId: string` — 消息 ID
  - `scope: RetryScope` — 重试范围，默认 `'all'`
    - `'all'` — 重新生成全部内容
    - `'failed_tools'` — 仅重试失败的工具调用
    - `'specific_tool'` — 重试特定工具调用（需提供 `targetToolId`）
  - `targetToolId?: string` — 目标工具 ID，当 `scope='specific_tool'` 时必需
- **返回**：`Promise<void>`
- **说明**：
  - **临时消息（`temp-` 开头）不能重试**，调用时将显示"消息正在保存中,请稍后重试"并直接返回
  - 404 或消息不存在时显示"消息不存在，无法重试"
  - 成功时根据 scope 显示不同的 toast 提示（如"开始重新生成全部内容"）

#### createMessageVersion

创建消息的版本快照，用于版本历史记录。

- **参数**：
  - `messageId: string` — 消息 ID
- **返回**：`number` — 新创建的版本号
- **说明**：成功时 toast 提示"已创建版本 {version}"。内部委托 `sessionStore.createMessageVersion`。

#### restoreMessageVersion

将消息恢复到指定版本。

- **参数**：
  - `messageId: string` — 消息 ID
  - `version: number` — 目标版本号
- **返回**：`void`
- **说明**：成功时 toast 提示"已恢复到版本 {version}"。内部委托 `sessionStore.restoreMessageVersion`。

#### getMessageVersions

获取指定消息的所有版本列表。

- **参数**：
  - `messageId: string` — 消息 ID
- **返回**：`Promise<unknown[]>` — 版本列表
- **说明**：
  - 临时消息（`temp-` 开头）直接返回空数组
  - 404 错误和网络错误静默处理，返回空数组
  - 其他错误显示 toast 提示并抛出异常

## 使用示例

```tsx
import { useMessageActions } from '@/hooks/useMessageActions'

function MessageActionsPanel({ sessionId, messageId }: { sessionId: string; messageId: string }) {
  const {
    editMessage,
    deleteMessage,
    retryMessageWithScope,
    createMessageVersion,
    restoreMessageVersion,
    getMessageVersions,
  } = useMessageActions(sessionId)

  const handleEdit = async () => {
    try {
      await editMessage(messageId, '编辑后的新内容')
    } catch {
      // 错误已由 Hook 内部处理（toast + 上报）
    }
  }

  const handleDelete = async () => {
    try {
      await deleteMessage(messageId, true)
    } catch {
      // 错误已由 Hook 内部处理
    }
  }

  const handleRetryAll = async () => {
    try {
      await retryMessageWithScope(messageId, 'all')
    } catch {
      // 错误已由 Hook 内部处理
    }
  }

  const handleRetryFailedTools = async () => {
    try {
      await retryMessageWithScope(messageId, 'failed_tools')
    } catch {
      // 错误已由 Hook 内部处理
    }
  }

  const handleRetrySpecificTool = async (toolId: string) => {
    try {
      await retryMessageWithScope(messageId, 'specific_tool', toolId)
    } catch {
      // 错误已由 Hook 内部处理
    }
  }

  const handleCreateVersion = () => {
    const version = createMessageVersion(messageId)
    console.log('已创建版本:', version)
  }

  const handleRestoreVersion = (version: number) => {
    restoreMessageVersion(messageId, version)
  }

  const handleLoadVersions = async () => {
    const versions = await getMessageVersions(messageId)
    console.log('版本列表:', versions)
  }

  return (
    <div className="flex gap-2">
      <button onClick={handleEdit}>编辑</button>
      <button onClick={handleDelete}>删除</button>
      <button onClick={handleRetryAll}>全部重试</button>
      <button onClick={handleRetryFailedTools}>重试失败工具</button>
      <button onClick={handleCreateVersion}>创建版本</button>
    </div>
  )
}
```

## 依赖关系

| 依赖 | 说明 |
|------|------|
| `react` (`useCallback`) | Hook 基础 |
| `sonner` (`toast`) | 操作结果提示 |
| `@/services/api/messages` (`messageApi`) | 消息 API 调用（编辑、获取版本） |
| `@/services/errorReporting` (`reportError`, `ErrorType`) | 错误上报 |
| `@/stores/sessionStore` (`useSessionStore`) | 会话状态管理（删除、重试、版本操作） |
| `@/types/models` (`RetryScope`) | 重试范围类型定义 |

## 注意事项

1. **sessionId 必须传入**：所有方法在 `sessionId` 为空时将抛出 `Error('sessionId is required for ...')`，建议在调用前确认 `sessionId` 有效。
2. **临时消息限制**：`retryMessageWithScope` 和 `getMessageVersions` 对 `temp-` 开头的消息 ID 有特殊处理，临时消息不能重试，获取版本时返回空数组。
3. **错误处理策略**：Hook 内部已集成 toast 提示和错误上报，调用方无需重复处理 UI 提示，但需自行 `try/catch` 以处理业务逻辑层面的失败。
4. **useCallback 依赖**：所有返回方法均通过 `useCallback` 缓存，依赖 `sessionId` 和 `sessionStore`，当依赖变化时会重新创建。
5. **deleteMessage 为异步操作**：内部采用乐观更新策略（先更新 UI，再调用 API），若 API 失败会自动回滚状态。
6. **retryMessageWithScope 的 scope 参数**：使用 `'specific_tool'` 时必须同时提供 `targetToolId`，否则仅调用默认重试。
