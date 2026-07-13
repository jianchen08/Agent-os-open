# useLayoutModeStore

## 用途

Zustand + persist 布局模式管理 Store。管理 classic/five-space 布局切换，以及五空间布局的浮动窗口、工作区标签、Dock 栏、全屏遮罩、执行事件、交互请求和连接状态。

## API

### 状态

| 属性 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | `LayoutMode` | `'classic'` | 布局模式（classic / five-space） |
| `floatingWindows` | `FloatingWindowInstance[]` | `[]` | 浮动窗口实例列表 |
| `workspaceTabs` | `WorkspaceTab[]` | `[]` | 工作区标签列表 |
| `dockItems` | `DockItem[]` | `[]` | Dock 栏项目列表 |
| `fullscreenActive` | `boolean` | `false` | 全屏遮罩是否激活 |
| `fullscreenTitle` | `string \| null` | `null` | 全屏标题 |
| `fullscreenContent` | `ReactNode \| null` | `null` | 全屏内容 |
| `activeExecutions` | `ExecutionEvent[]` | `[]` | 活跃执行事件列表 |
| `pendingInteractions` | `InteractionRequest[]` | `[]` | 待处理交互请求 |
| `connectionStatus` | `ConnectionStatus` | 见下方 | WebSocket 连接状态 |

### ConnectionStatus 默认值

```ts
{
  state: 'disconnected',       // connected | connecting | reconnecting | disconnected | failed
  latencyMs: null,             // 延迟毫秒数
  reconnectAttempt: 0,         // 重连次数
  lastConnectedAt: null,       // 最后连接时间
  queuedMessages: 0,           // 队列消息数
}
```

### 方法

#### 布局模式

| 方法 | 签名 | 说明 |
|------|------|------|
| `toggleMode` | `() => void` | 切换 classic / five-space |
| `setMode` | `(mode: LayoutMode) => void` | 设置指定布局模式 |

#### 浮动窗口管理

| 方法 | 签名 | 说明 |
|------|------|------|
| `addFloatingWindow` | `(window: FloatingWindowInstance) => void` | 添加浮动窗口 |
| `updateFloatingWindow` | `(id, updates) => void` | 更新浮动窗口属性 |
| `closeFloatingWindow` | `(id: string) => void` | 关闭（移除）浮动窗口 |
| `minimizeFloatingWindow` | `(id: string) => void` | 最小化浮动窗口 |
| `restoreFloatingWindow` | `(id: string) => void` | 恢复浮动窗口 |

#### 工作区标签管理

| 方法 | 签名 | 说明 |
|------|------|------|
| `addWorkspaceTab` | `(tab: WorkspaceTab) => void` | 添加工作区标签 |
| `setActiveTab` | `(tabId: string) => void` | 设置活跃标签 |
| `closeWorkspaceTab` | `(tabId: string) => void` | 关闭工作区标签 |
| `updateWorkspaceTab` | `(tabId, updates) => void` | 更新工作区标签属性 |

#### Dock 栏管理

| 方法 | 签名 | 说明 |
|------|------|------|
| `setDockItems` | `(items: DockItem[]) => void` | 设置 Dock 栏所有项目 |
| `updateDockItem` | `(id, updates) => void` | 更新单个 Dock 项目 |

#### 全屏遮罩

| 方法 | 签名 | 说明 |
|------|------|------|
| `enterFullscreen` | `(title: string, content: ReactNode) => void` | 进入全屏 |
| `exitFullscreen` | `() => void` | 退出全屏 |

#### 执行事件管理

| 方法 | 签名 | 说明 |
|------|------|------|
| `addOrUpdateExecution` | `(event: ExecutionEvent) => void` | 添加或更新执行事件 |
| `removeExecution` | `(id: string) => void` | 移除执行事件 |
| `clearCompletedExecutions` | `() => void` | 清除所有已完成的执行事件 |

#### 交互请求管理

| 方法 | 签名 | 说明 |
|------|------|------|
| `addInteraction` | `(request: InteractionRequest) => void` | 添加交互请求 |
| `removeInteraction` | `(id: string) => void` | 移除交互请求 |

#### 连接状态

| 方法 | 签名 | 说明 |
|------|------|------|
| `updateConnectionStatus` | `(status: Partial<ConnectionStatus>) => void` | 更新连接状态（浅合并） |

## 使用示例

```tsx
import { useLayoutModeStore } from '@/stores/layoutModeStore'

function LayoutToggle() {
  const { mode, toggleMode } = useLayoutModeStore()

  return (
    <button onClick={toggleMode}>
      当前: {mode === 'classic' ? '经典' : '五空间'}布局
    </button>
  )
}
```

## 依赖关系

| 依赖 | 类型 | 说明 |
|------|------|------|
| `zustand` + `persist` | 状态管理 | 仅持久化 `mode` |
| `FloatingWindowInstance` | 类型 | 浮动窗口实例类型（来自 `@/types/layout`） |
| `WorkspaceTab` | 类型 | 工作区标签类型 |
| `DockItem` | 类型 | Dock 项目类型 |

### 持久化策略

使用 `zustand/persist`，键名 `layout-mode`，仅持久化 `mode` 字段。

## 注意事项

1. **fullscreenContent 类型**：使用 `ReactNode`，注意序列化限制（不持久化）
2. **执行事件自动清理**：`useRealtimeEvents` Hook 会在完成/取消后延迟移除执行事件
3. **connectionStatus 浅合并**：`updateConnectionStatus` 使用展开运算符合并，非深度合并
4. **classic 模式**：五空间相关状态在 classic 模式下仍保留（不重置）
