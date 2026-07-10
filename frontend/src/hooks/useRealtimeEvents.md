# useRealtimeEvents

## 用途

订阅所有实时 WebSocket 事件，将其路由到 `layoutModeStore` 以在五空间布局中展示。

顶层组件（如 `FiveSpaceHomePage`）中调用一次即可，无需传参、无需返回值。

## API

### 参数

无

### 返回值

`void` — 本 Hook 不返回任何值

### 订阅的事件

| 事件类别 | 事件名 | 说明 |
|----------|--------|------|
| 流式输出 | `STREAM_START` | 流式开始，更新连接状态为 connected |
| 流式输出 | `STREAM_CHUNK` | 流式分块（由 sessionStore 处理，本 Hook 忽略） |
| 流式输出 | `STREAM_END` | 流式结束 |
| 流式输出 | `STREAM_ERROR` | 流式错误 |
| 执行进度 | `EXECUTION_START` | 创建 ExecutionEvent（status=running），写入 activeExecutions |
| 执行进度 | `EXECUTION_PROGRESS` | 更新已有执行记录的 progress |
| 执行进度 | `EXECUTION_OUTPUT` | 追加或替换执行记录的 output |
| 执行进度 | `EXECUTION_DONE` | 标记执行完成（completed/failed），10秒后自动移除 |
| 执行进度 | `EXECUTION_CANCELLED` | 标记执行取消，5秒后自动移除 |
| 子代理 | `SUB_AGENT_CREATED` | 创建 agent 类型 ExecutionEvent |
| 子代理 | `SUB_AGENT_WAITING_INPUT` | 添加 InteractionRequest 到 pendingInteractions |
| 子代理 | `SUB_AGENT_COMPLETED` | 标记子代理完成，10秒后自动移除 |
| 工作流 | `WORKFLOW_STEP_UPDATE` | 工作流步骤更新（预留，暂无具体逻辑） |

## 使用示例

```tsx
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents'

function FiveSpaceHomePage() {
  // 顶层调用一次，自动管理 WebSocket 事件订阅
  useRealtimeEvents()

  return <FiveSpaceLayout>...</FiveSpaceLayout>
}
```

## 依赖关系

| 依赖 | 类型 | 说明 |
|------|------|------|
| `webSocketService` | Service | WebSocket 服务实例，用于 subscribe/unsubscribe |
| `WS_SERVER_EVENTS` | Constant | WebSocket 事件名常量 |
| `useLayoutModeStore` | Zustand Store | 接收路由后的执行事件和交互请求 |

### 调用的 layoutModeStore 方法

| 方法 | 用途 |
|------|------|
| `addOrUpdateExecution` | 添加或更新执行记录 |
| `removeExecution` | 移除已完成的执行记录（延迟 5-10 秒） |
| `addInteraction` | 添加交互请求 |
| `removeInteraction` | 移除交互请求 |
| `updateConnectionStatus` | 更新 WebSocket 连接状态 |

## 注意事项

1. **只调用一次**：应在应用最顶层组件中调用，避免重复订阅
2. **自动清理**：useEffect 返回的 cleanup 函数会在组件卸载时取消所有订阅
3. **延迟移除**：完成的执行记录会在 10 秒后自动移除，取消的 5 秒后移除
4. **事件处理委托**：流式文本块（STREAM_CHUNK）由 sessionStore 直接处理，本 Hook 不处理
